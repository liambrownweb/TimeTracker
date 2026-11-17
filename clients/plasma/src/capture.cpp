#include "capture.h"

#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QRegularExpression>
#include <QSaveFile>
#include <QSet>
#include <QStandardPaths>
#include <QUuid>
#include <QUrlQuery>
#include <QtMath>
#include <fcntl.h>
#include <cmath>
#include <limits>
#include <time.h>
#include <unistd.h>

namespace {
qint64 systemMonotonic()
{
    timespec ts{};
    if (clock_gettime(CLOCK_BOOTTIME, &ts) != 0)
        return -1;
    return qint64(ts.tv_sec) * 1000 + ts.tv_nsec / 1000000;
}

QString systemBootId()
{
    QFile file(QStringLiteral("/proc/sys/kernel/random/boot_id"));
    return file.open(QIODevice::ReadOnly) ? QString::fromUtf8(file.readAll()).trimmed() : QString();
}

QString uuid()
{
    return QUuid::createUuid().toString(QUuid::WithoutBraces);
}

QString utc(qint64 ms)
{
    return QDateTime::fromMSecsSinceEpoch(ms, Qt::UTC).toString(Qt::ISODateWithMs);
}

bool syncDirectory(const QString &path)
{
    const QByteArray name = QFile::encodeName(path);
    const int fd = ::open(name.constData(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) return false;
    const bool ok = ::fsync(fd) == 0;
    ::close(fd);
    return ok;
}

bool createDirectories(const QString &path)
{
    const QString absolute = QDir::cleanPath(QFileInfo(path).absoluteFilePath());
    QString current = QStringLiteral("/");
    for (const QString &part : absolute.split('/', Qt::SkipEmptyParts)) {
        const QString child = QDir(current).filePath(part);
        if (!QFileInfo::exists(child)) {
            if (!QDir().mkdir(child) || !syncDirectory(current))
                return false;
        }
        if (!QFileInfo(child).isDir()) return false;
        current = child;
    }
    return true;
}

bool integer(const QJsonValue &value, double minimum, double maximum)
{
    return value.isDouble() && value.toDouble() >= minimum && value.toDouble() <= maximum
        && std::floor(value.toDouble()) == value.toDouble();
}

bool validTimestamp(const QJsonValue &value)
{
    if (!value.isString()) return false;
    const auto time = QDateTime::fromString(value.toString(), Qt::ISODate);
    return time.isValid() && time.timeSpec() != Qt::LocalTime;
}

bool validId(const QJsonValue &value)
{
    return value.isString() && !QUuid(value.toString()).isNull();
}

bool validPayload(const QJsonObject &obj, double maximumDuration = 360000000000.0)
{
    return validId(obj.value("id")) && obj.value("task").isString()
        && !obj.value("task").toString().trimmed().isEmpty()
        && integer(obj.value("duration_seconds"), 0, maximumDuration)
        && (obj.value("started_at").isNull() || validTimestamp(obj.value("started_at")))
        && obj.value("attributes").isObject();
}

bool validResponse(const QJsonObject &obj, const QString &id)
{
    return validPayload(obj, std::numeric_limits<double>::max())
        && QUuid(obj.value("id").toString()) == QUuid(id)
        && obj.value("deleted").isBool()
        && integer(obj.value("revision"), 1, std::numeric_limits<double>::max())
        && validTimestamp(obj.value("created_at")) && validTimestamp(obj.value("updated_at"));
}
}

Capture::Capture(QObject *parent)
    : QObject(parent), m_bootId(systemBootId()),
      m_wall([] { return QDateTime::currentMSecsSinceEpoch(); }),
      m_mono(systemMonotonic)
{
    connect(&m_timer, &QTimer::timeout, this, &Capture::tick);
    m_timer.start(1000);
}

Capture::Capture(const QString &path, std::function<qint64()> wall,
                 std::function<qint64()> monotonic, const QString &bootId, QObject *parent)
    : QObject(parent), m_bootId(bootId), m_wall(std::move(wall)), m_mono(std::move(monotonic))
{
    connect(&m_timer, &QTimer::timeout, this, &Capture::tick);
    m_timer.start(1000);
    open(path);
}

bool Capture::fail(const QString &message)
{
    m_error = message;
    emit errorChanged();
    return false;
}

void Capture::clearError()
{
    m_error.clear();
    emit errorChanged();
}

bool Capture::initialize(const QString &instance)
{
    if (m_ready)
        return true;
    if (!QRegularExpression(QStringLiteral("^[0-9]+$")).match(instance).hasMatch())
        return fail(QStringLiteral("Invalid widget instance identifier."));
    const QString root = QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation);
    if (root.isEmpty())
        return fail(QStringLiteral("Cannot locate the local data directory."));
    return open(root + QStringLiteral("/timetracker/plasma/") + instance + QStringLiteral("/state.json"));
}

bool Capture::open(const QString &path)
{
    if (m_bootId.isEmpty() || m_mono() < 0)
        return fail(QStringLiteral("Cannot access the system boot ID or monotonic clock."));
    QDir dir(QFileInfo(path).absolutePath());
    if (!createDirectories(dir.absolutePath()))
        return fail(QStringLiteral("Cannot create the local data directory."));
    m_path = path;
    m_lock = std::make_unique<QLockFile>(path + QStringLiteral(".lock"));
    m_lock->setStaleLockTime(0);
    if (!m_lock->tryLock())
        return fail(QStringLiteral("This widget's local data is already in use or cannot be locked."));
    QFile file(path);
    if (file.exists()) {
        if (!file.open(QIODevice::ReadOnly))
            return fail(QStringLiteral("Cannot read local state: %1").arg(file.errorString()));
        QJsonParseError parse;
        const auto doc = QJsonDocument::fromJson(file.readAll(), &parse);
        const auto obj = doc.object();
        if (parse.error != QJsonParseError::NoError || !doc.isObject()
            || obj.value("version").toInt() != 1 || !obj.value("drafts").isArray()
            || !obj.value("pending").isArray() || !validUrl(obj.value("service").toString()))
            return fail(QStringLiteral("Local state is invalid or has an unsupported version; it was not overwritten."));
        QSet<QUuid> ids;
        int running = 0;
        bool valid = true;
        for (const auto &value : obj.value("drafts").toArray()) {
            const auto draft = value.toObject();
            const auto id = draft.value("id");
            const bool active = draft.value("running").toBool();
            valid = valid && value.isObject() && validId(id) && !ids.contains(QUuid(id.toString()))
                && draft.value("task").isString() && draft.value("attributes_json").isString()
                && QJsonDocument::fromJson(draft.value("attributes_json").toString().toUtf8()).isObject()
                && (draft.value("started_at").isNull() || validTimestamp(draft.value("started_at")))
                && integer(draft.value("elapsed_ms"), 0, 360000000000000.0)
                && draft.value("running").isBool() && draft.value("uncertain").isBool();
            if (active) {
                ++running;
                valid = valid && integer(draft.value("wall_ms"), 0, 9007199254740991.0)
                    && integer(draft.value("mono_ms"), 0, 9007199254740991.0)
                    && draft.value("boot_id").isString() && !draft.value("boot_id").toString().isEmpty();
            }
            ids.insert(QUuid(id.toString()));
        }
        for (const auto &value : obj.value("pending").toArray()) {
            const auto item = value.toObject();
            const auto id = item.value("id");
            const auto payload = QJsonDocument::fromJson(item.value("payload").toString().toUtf8());
            const QString suffix = QStringLiteral(",\"attributes\":")
                + item.value("attributes_json").toString() + QLatin1Char('}');
            const QString status = item.value("status").toString();
            valid = valid && value.isObject() && validId(id) && !ids.contains(QUuid(id.toString()))
                && validUrl(item.value("destination").toString()) && item.value("payload").isString()
                && item.value("attributes_json").isString()
                && QJsonDocument::fromJson(item.value("attributes_json").toString().toUtf8()).isObject()
                && payload.isObject() && validPayload(payload.object())
                && item.value("payload").toString().endsWith(suffix)
                && QUuid(payload.object().value("id").toString()) == QUuid(id.toString())
                && (status == "waiting" || status == "error") && item.value("message").isString()
                && integer(item.value("attempts"), 0, 16)
                && integer(item.value("next_ms"), 0, 9007199254740991.0);
            ids.insert(QUuid(id.toString()));
        }
        if (!valid || running > 1)
            return fail(QStringLiteral("Local state contains invalid draft or queue data; it was not overwritten."));
        m_state = obj;
    } else {
        m_state = QJsonObject{{"version", 1}, {"service", "http://127.0.0.1:8000"},
                              {"drafts", QJsonArray{}}, {"pending", QJsonArray{}}};
        if (!persist(m_state))
            return false;
    }
    m_ready = true;
    tick();
    emit changed();
    return true;
}

bool Capture::persist(const QJsonObject &next)
{
    QSaveFile file(m_path);
    if (!file.open(QIODevice::WriteOnly))
        return fail(QStringLiteral("Cannot write local state: %1").arg(file.errorString()));
    if (!file.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner)) {
        file.cancelWriting();
        return fail(QStringLiteral("Cannot restrict local state permissions."));
    }
    const QByteArray bytes = QJsonDocument(next).toJson(QJsonDocument::Compact);
    if (file.write(bytes) != bytes.size() || !file.flush() || ::fsync(file.handle()) != 0) {
        file.cancelWriting();
        return fail(QStringLiteral("Cannot flush local state; the action was not saved."));
    }
    if (!file.commit())
        return fail(QStringLiteral("Cannot commit local state: %1").arg(file.errorString()));
    if (!syncDirectory(QFileInfo(m_path).absolutePath())) {
        m_ready = false;
        emit changed();
        return fail(QStringLiteral("Cannot confirm local state durability. Restart the widget before continuing."));
    }
    m_state = next;
    emit changed();
    return true;
}

bool Capture::validUrl(const QString &text)
{
    const QUrl url(text, QUrl::StrictMode);
    return url.isValid() && (url.scheme() == "http" || url.scheme() == "https")
        && !url.host().isEmpty() && url.userInfo().isEmpty()
        && !url.hasQuery() && !url.hasFragment();
}

QUrl Capture::endpoint(const QString &base, const QString &path)
{
    QUrl url(base);
    QString prefix = url.path();
    while (prefix.endsWith('/'))
        prefix.chop(1);
    url.setPath(prefix + path);
    return url;
}

QString Capture::serviceUrl() const { return m_state.value("service").toString(); }

bool Capture::setServiceUrl(const QString &url)
{
    if (!m_ready) return fail(QStringLiteral("Local state is unavailable."));
    const QString clean = url.trimmed();
    if (!validUrl(clean))
        return fail(QStringLiteral("Enter an HTTP or HTTPS service URL without credentials, query, or fragment."));
    auto next = m_state;
    next["service"] = clean;
    if (!persist(next)) return false;
    ++m_completionGeneration;
    m_suggestions.clear();
    emit suggestionsChanged();
    return true;
}

qint64 Capture::elapsed(const QJsonObject &draft, qint64 wall, qint64 mono, bool *uncertain) const
{
    *uncertain = draft.value("uncertain").toBool();
    qint64 result = qint64(draft.value("elapsed_ms").toDouble());
    if (!draft.value("running").toBool())
        return result;
    const qint64 wallDelta = wall - qint64(draft.value("wall_ms").toDouble());
    qint64 delta = wallDelta;
    if (draft.value("boot_id").toString() == m_bootId) {
        delta = mono - qint64(draft.value("mono_ms").toDouble());
        if (delta < 0 || qAbs(wallDelta - delta) > 2000)
            *uncertain = true;
    } else if (wallDelta < 0) {
        *uncertain = true;
    }
    if (delta < 0)
        delta = 0;
    return result + delta;
}

QVariantList Capture::drafts() const
{
    QVariantList list;
    const qint64 wall = m_wall(), mono = m_mono();
    for (const auto &value : m_state.value("drafts").toArray()) {
        auto item = value.toObject();
        bool uncertain = false;
        const qint64 ms = elapsed(item, wall, mono, &uncertain);
        auto map = item.toVariantMap();
        map["display_seconds"] = ms / 1000;
        map["uncertain"] = uncertain;
        list.append(map);
    }
    return list;
}

QVariantList Capture::pending() const
{
    QVariantList list;
    for (const auto &value : m_state.value("pending").toArray()) {
        auto map = value.toObject().toVariantMap();
        const auto payload = QJsonDocument::fromJson(map.value("payload").toString().toUtf8()).object();
        map["task"] = payload.value("task").toString();
        list.append(map);
    }
    return list;
}

int Capture::findDraft(const QString &id) const
{
    const auto items = m_state.value("drafts").toArray();
    for (int i = 0; i < items.size(); ++i)
        if (items.at(i).toObject().value("id").toString() == id) return i;
    return -1;
}

int Capture::findPending(const QString &id) const
{
    const auto items = m_state.value("pending").toArray();
    for (int i = 0; i < items.size(); ++i)
        if (items.at(i).toObject().value("id").toString() == id) return i;
    return -1;
}

bool Capture::addDraft()
{
    if (!m_ready) return fail(QStringLiteral("Local state is unavailable."));
    auto next = m_state;
    auto items = next.value("drafts").toArray();
    items.append(QJsonObject{{"id", uuid()}, {"task", ""}, {"attributes_json", "{}"},
                             {"started_at", QJsonValue::Null}, {"elapsed_ms", 0},
                             {"running", false}, {"uncertain", false}});
    next["drafts"] = items;
    return persist(next);
}

bool Capture::saveDraft(const QString &id, const QString &task, const QString &attributes,
                        const QString &duration, bool reviewed)
{
    if (!m_ready) return fail(QStringLiteral("Local state is unavailable."));
    const int index = findDraft(id);
    if (index < 0) return fail(QStringLiteral("Draft no longer exists."));
    auto items = m_state.value("drafts").toArray();
    auto draft = items.at(index).toObject();
    if (draft.value("running").toBool())
        return fail(QStringLiteral("Pause the timer before editing the draft."));
    QJsonParseError parse;
    const auto doc = QJsonDocument::fromJson(attributes.toUtf8(), &parse);
    if (parse.error != QJsonParseError::NoError || !doc.isObject())
        return fail(QStringLiteral("Metadata must be a JSON object."));
    const auto match = QRegularExpression(QStringLiteral("^(\\d{1,8}):([0-5]\\d):([0-5]\\d)$")).match(duration);
    if (!match.hasMatch())
        return fail(QStringLiteral("Duration must use HH:MM:SS with nonnegative hours."));
    const qint64 seconds = match.captured(1).toLongLong() * 3600
        + match.captured(2).toLongLong() * 60 + match.captured(3).toLongLong();
    if (draft.value("uncertain").toBool() && !reviewed)
        return fail(QStringLiteral("Review and confirm the uncertain duration."));
    draft["task"] = task;
    draft["attributes_json"] = attributes;
    // Preserve fractional milliseconds if the displayed whole seconds were not changed.
    const qint64 previous = qint64(draft.value("elapsed_ms").toDouble());
    draft["elapsed_ms"] = double(seconds * 1000 + (previous / 1000 == seconds ? previous % 1000 : 0));
    if (reviewed) draft["uncertain"] = false;
    items[index] = draft;
    auto next = m_state;
    next["drafts"] = items;
    return persist(next);
}

bool Capture::settle(QJsonObject &draft, qint64 wall, qint64 mono)
{
    if (!draft.value("running").toBool()) return true;
    bool uncertain = false;
    const qint64 ms = elapsed(draft, wall, mono, &uncertain);
    draft["elapsed_ms"] = double(ms);
    draft["uncertain"] = uncertain;
    draft["running"] = false;
    return true;
}

bool Capture::start(const QString &id)
{
    if (!m_ready) return fail(QStringLiteral("Local state is unavailable."));
    const int index = findDraft(id);
    if (index < 0) return fail(QStringLiteral("Draft no longer exists."));
    const qint64 wall = m_wall(), mono = m_mono();
    if (mono < 0) return fail(QStringLiteral("Monotonic clock is unavailable."));
    auto items = m_state.value("drafts").toArray();
    if (items.at(index).toObject().value("running").toBool()) return true;
    for (int i = 0; i < items.size(); ++i) {
        auto draft = items.at(i).toObject();
        settle(draft, wall, mono);
        if (i == index) {
            if (draft.value("uncertain").toBool())
                return fail(QStringLiteral("Review the uncertain duration before restarting."));
            draft["running"] = true;
            draft["wall_ms"] = double(wall);
            draft["mono_ms"] = double(mono);
            draft["boot_id"] = m_bootId;
            if (draft.value("started_at").isNull())
                draft["started_at"] = utc(wall);
        }
        items[i] = draft;
    }
    auto next = m_state;
    next["drafts"] = items;
    return persist(next);
}

bool Capture::pause(const QString &id)
{
    if (!m_ready) return fail(QStringLiteral("Local state is unavailable."));
    const int index = findDraft(id);
    if (index < 0) return fail(QStringLiteral("Draft no longer exists."));
    const qint64 mono = m_mono();
    if (mono < 0) return fail(QStringLiteral("Monotonic clock is unavailable."));
    auto items = m_state.value("drafts").toArray();
    auto draft = items.at(index).toObject();
    settle(draft, m_wall(), mono);
    items[index] = draft;
    auto next = m_state;
    next["drafts"] = items;
    return persist(next);
}

bool Capture::removeDraft(const QString &id)
{
    if (!m_ready) return fail(QStringLiteral("Local state is unavailable."));
    const int index = findDraft(id);
    if (index < 0) return fail(QStringLiteral("Draft no longer exists."));
    auto next = m_state;
    auto items = next.value("drafts").toArray();
    items.removeAt(index);
    next["drafts"] = items;
    return persist(next);
}

bool Capture::submit(const QString &id)
{
    if (!m_ready) return fail(QStringLiteral("Local state is unavailable."));
    const int index = findDraft(id);
    if (index < 0) return fail(QStringLiteral("Draft no longer exists."));
    auto items = m_state.value("drafts").toArray();
    const auto draft = items.at(index).toObject();
    if (draft.value("running").toBool()) return fail(QStringLiteral("Pause the timer before submitting."));
    if (draft.value("uncertain").toBool()) return fail(QStringLiteral("Review the uncertain duration before submitting."));
    if (draft.value("task").toString().trimmed().isEmpty()) return fail(QStringLiteral("Enter a task before submitting."));
    if (!validUrl(serviceUrl())) return fail(QStringLiteral("Configure a valid service URL before submitting."));
    const QString requestId = uuid();
    const QJsonObject payload{{"id", requestId}, {"task", draft.value("task")},
                              {"duration_seconds", qint64(draft.value("elapsed_ms").toDouble()) / 1000},
                              {"started_at", draft.value("started_at")}};
    QByteArray payloadBytes = QJsonDocument(payload).toJson(QJsonDocument::Compact);
    payloadBytes.chop(1);
    payloadBytes += ",\"attributes\":" + draft.value("attributes_json").toString().toUtf8() + "}";
    auto next = m_state;
    auto queue = next.value("pending").toArray();
    queue.append(QJsonObject{{"id", requestId}, {"destination", serviceUrl()},
                             {"payload", QString::fromUtf8(payloadBytes)},
                             {"attributes_json", draft.value("attributes_json")},
                             {"status", "waiting"}, {"message", ""}, {"attempts", 0}, {"next_ms", 0}});
    items.removeAt(index);
    next["drafts"] = items;
    next["pending"] = queue;
    if (!persist(next)) return false;
    deliver();
    return true;
}

bool Capture::retry(const QString &id)
{
    if (!m_ready) return fail(QStringLiteral("Local state is unavailable."));
    const int index = findPending(id);
    if (index < 0) return fail(QStringLiteral("Pending request no longer exists."));
    auto next = m_state;
    auto queue = next.value("pending").toArray();
    auto item = queue.at(index).toObject();
    item["status"] = "waiting";
    item["next_ms"] = 0;
    item["message"] = "";
    queue[index] = item;
    next["pending"] = queue;
    if (!persist(next)) return false;
    deliver();
    return true;
}

bool Capture::discardPending(const QString &id)
{
    if (!m_ready) return fail(QStringLiteral("Local state is unavailable."));
    if (m_inFlight) return fail(QStringLiteral("Wait for the current request to finish before discarding."));
    const int index = findPending(id);
    if (index < 0) return fail(QStringLiteral("Pending request no longer exists."));
    auto next = m_state;
    auto queue = next.value("pending").toArray();
    queue.removeAt(index);
    next["pending"] = queue;
    return persist(next);
}

bool Capture::copyPending(const QString &id)
{
    if (!m_ready) return fail(QStringLiteral("Local state is unavailable."));
    const int index = findPending(id);
    if (index < 0) return fail(QStringLiteral("Pending request no longer exists."));
    const auto item = m_state.value("pending").toArray().at(index).toObject();
    const auto payload = QJsonDocument::fromJson(item.value("payload").toString().toUtf8()).object();
    if (!validPayload(payload)) return fail(QStringLiteral("The pending request is invalid."));
    auto next = m_state;
    auto items = next.value("drafts").toArray();
    items.append(QJsonObject{{"id", uuid()}, {"task", payload.value("task")},
                             {"attributes_json", item.value("attributes_json")},
                             {"started_at", payload.value("started_at")},
                             {"elapsed_ms", payload.value("duration_seconds").toDouble() * 1000},
                             {"running", false}, {"uncertain", false}});
    next["drafts"] = items;
    return persist(next);
}

void Capture::tick()
{
    if (!m_ready) return;
    auto items = m_state.value("drafts").toArray();
    bool changedState = false;
    bool clockChanged = false;
    for (int i = 0; i < items.size(); ++i) {
        auto draft = items.at(i).toObject();
        bool uncertain = false;
        if (draft.value("running").toBool()) {
            const qint64 mono = m_mono();
            if (mono < 0) {
                fail(QStringLiteral("Monotonic clock is unavailable; timer cannot be trusted."));
                return;
            }
            elapsed(draft, m_wall(), mono, &uncertain);
            if (uncertain) {
                settle(draft, m_wall(), mono);
                items[i] = draft;
                changedState = true;
                clockChanged = true;
            } else if (draft.value("boot_id").toString() != m_bootId) {
                draft["elapsed_ms"] = double(elapsed(draft, m_wall(), mono, &uncertain));
                draft["wall_ms"] = double(m_wall());
                draft["mono_ms"] = double(mono);
                draft["boot_id"] = m_bootId;
                items[i] = draft;
                changedState = true;
            }
        }
    }
    if (changedState) {
        auto next = m_state;
        next["drafts"] = items;
        if (!persist(next)) return;
        if (clockChanged)
            fail(QStringLiteral("A clock change was detected. Review the paused duration."));
    } else {
        emit changed();
    }
    deliver();
}

void Capture::deliver()
{
    if (!m_ready || m_inFlight) return;
    for (const auto &value : m_state.value("pending").toArray()) {
        const auto item = value.toObject();
        if (item.value("status").toString() != "waiting"
            || item.value("next_ms").toDouble() > m_wall()) continue;
        if (!validUrl(item.value("destination").toString())) {
            fail(QStringLiteral("A pending request has an invalid destination and cannot be sent."));
            return;
        }
        QNetworkRequest request(endpoint(item.value("destination").toString(), "/v0/entries"));
        request.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
        request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::ManualRedirectPolicy);
        m_inFlight = true;
        QNetworkReply *reply = m_network.post(request, item.value("payload").toString().toUtf8());
        QTimer::singleShot(15000, reply, [reply] { if (reply->isRunning()) reply->abort(); });
        const QString id = item.value("id").toString();
        connect(reply, &QNetworkReply::finished, this, [this, id, reply] { finished(id, reply); });
        return;
    }
}

void Capture::finished(const QString &id, QNetworkReply *reply)
{
    m_inFlight = false;
    const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
    const auto replyError = reply->error();
    const QByteArray body = reply->readAll();
    const QString networkError = reply->errorString();
    reply->deleteLater();
    if (!m_ready) return;
    const int index = findPending(id);
    if (index < 0) { fail(QStringLiteral("A pending request disappeared during delivery.")); return; }
    auto next = m_state;
    auto queue = next.value("pending").toArray();
    auto item = queue.at(index).toObject();
    QJsonParseError parse;
    const auto doc = QJsonDocument::fromJson(body, &parse);
    if ((status == 200 || status == 201) && replyError == QNetworkReply::NoError
        && parse.error == QJsonParseError::NoError
        && doc.isObject() && validResponse(doc.object(), id)) {
        queue.removeAt(index);
    } else {
        const bool permanent = (status >= 400 && status < 500 && status != 408 && status != 429)
            || (status >= 300 && status < 400);
        const int attempts = qMin(item.value("attempts").toInt() + 1, 16);
        item["attempts"] = attempts;
        item["status"] = permanent ? "error" : "waiting";
        const int delay = qMin(300, 1 << qMin(attempts, 8));
        item["next_ms"] = double(m_wall() + delay * 1000LL);
        item["message"] = status ? QStringLiteral("HTTP %1%2").arg(status).arg(
            status == 200 || status == 201 ? QStringLiteral(": invalid or mismatched acknowledgment") : QString())
            : networkError;
        queue[index] = item;
    }
    next["pending"] = queue;
    if (persist(next)) deliver();
}

void Capture::completeTask(const QString &prefix)
{
    const quint64 generation = ++m_completionGeneration;
    if (!m_ready || !validUrl(serviceUrl())) return;
    QUrl url = endpoint(serviceUrl(), "/v0/completions");
    QUrlQuery query;
    query.addQueryItem("field", "task");
    query.addQueryItem("q", prefix);
    url.setQuery(query);
    QNetworkRequest request(url);
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute, QNetworkRequest::ManualRedirectPolicy);
    auto *reply = m_network.get(request);
    QTimer::singleShot(10000, reply, [reply] { if (reply->isRunning()) reply->abort(); });
    connect(reply, &QNetworkReply::finished, this, [this, reply, generation] {
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        const auto doc = QJsonDocument::fromJson(reply->readAll());
        const QString error = reply->errorString();
        reply->deleteLater();
        if (generation != m_completionGeneration) return;
        if (status != 200 || !doc.isObject() || doc.object().value("field").toString() != "task"
            || !doc.object().value("values").isArray()) {
            m_suggestions.clear();
            emit suggestionsChanged();
            fail(QStringLiteral("Task suggestions unavailable: %1").arg(status ? QStringLiteral("HTTP %1 or invalid response").arg(status) : error));
            return;
        }
        QStringList result;
        for (const auto &value : doc.object().value("values").toArray())
            if (value.isString()) result.append(value.toString());
        m_suggestions = result;
        emit suggestionsChanged();
    });
}
