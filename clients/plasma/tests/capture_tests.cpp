#include "capture.h"

#include <QDir>
#include <QFile>
#include <QJsonDocument>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QQmlComponent>
#include <QQmlEngine>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTemporaryDir>
#include <QtTest>

class Service : public QTcpServer {
    Q_OBJECT
public:
    QList<int> statuses;
    QList<QByteArray> requests;
    bool mismatch = false;
    bool deleted = false;
    bool loseResponse = false;

    Service()
    {
        connect(this, &QTcpServer::newConnection, this, [this] {
            auto *socket = nextPendingConnection();
            auto *buffer = new QByteArray;
            connect(socket, &QTcpSocket::disconnected, socket, &QObject::deleteLater);
            connect(socket, &QObject::destroyed, this, [buffer] { delete buffer; });
            connect(socket, &QTcpSocket::readyRead, this, [this, socket, buffer] {
                buffer->append(socket->readAll());
                const int split = buffer->indexOf("\r\n\r\n");
                if (split < 0) return;
                const auto header = buffer->left(split).toLower();
                const auto match = QRegularExpression("content-length: (\\d+)").match(QString::fromLatin1(header));
                if (!match.hasMatch()) return;
                const int length = match.captured(1).toInt();
                if (buffer->size() < split + 4 + length) return;
                const QByteArray body = buffer->mid(split + 4, length);
                requests.append(body);
                if (loseResponse) {
                    loseResponse = false;
                    socket->write("HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n");
                    socket->disconnectFromHost();
                    return;
                }
                const int status = statuses.isEmpty() ? 201 : statuses.takeFirst();
                auto result = QJsonDocument::fromJson(body).object();
                if (mismatch) result["id"] = QUuid::createUuid().toString(QUuid::WithoutBraces);
                result["created_at"] = "2026-01-01T00:00:00Z";
                result["updated_at"] = "2026-01-01T00:00:00Z";
                result["revision"] = 1;
                result["deleted"] = deleted;
                const QByteArray response = QJsonDocument(result).toJson(QJsonDocument::Compact);
                socket->write("HTTP/1.1 " + QByteArray::number(status) + " Test\r\n"
                              "Content-Type: application/json\r\nContent-Length: "
                              + QByteArray::number(response.size()) + "\r\nConnection: close\r\n\r\n" + response);
                socket->disconnectFromHost();
            });
        });
        QVERIFY(listen(QHostAddress::LocalHost));
    }

    QString url() const { return QStringLiteral("http://127.0.0.1:%1").arg(serverPort()); }
};

class CaptureTests : public QObject {
    Q_OBJECT
private slots:
    void timersAndRestart();
    void clockDiscontinuity();
    void rebootRebasesClock();
    void offlineAndDestinations();
    void retriesAndAcknowledgments();
    void automaticRetryAndCopy();
    void rejectsCorruptState();
    void persistenceFailure();
    void liveServerIntegration();
    void pluginInstalledAfterFailedLoad();
};

static QString draftId(const Capture &capture, int index = 0)
{
    return capture.drafts().at(index).toMap().value("id").toString();
}

static qint64 seconds(const Capture &capture, int index)
{
    return capture.drafts().at(index).toMap().value("display_seconds").toLongLong();
}

void CaptureTests::timersAndRestart()
{
    QTemporaryDir dir;
    const QString path = dir.path() + "/state.json";
    qint64 wall = 1700000000000, mono = 100000;
    auto w = [&] { return wall; };
    auto m = [&] { return mono; };
    QString a, b;
    {
        Capture c(path, w, m, "boot");
        QVERIFY(c.ready());
        QVERIFY(c.addDraft());
        QVERIFY(c.addDraft());
        a = draftId(c);
        b = draftId(c, 1);
        QVERIFY(c.start(a));
        wall += 10123; mono += 10123;
        QVERIFY(c.start(b));
        QCOMPARE(seconds(c, 0), 10);
        wall += 5000; mono += 5000;
        QCOMPARE(seconds(c, 0), 10);
        QCOMPARE(seconds(c, 1), 5);
        QVERIFY(c.start(a));
        QVERIFY(!c.drafts()[1].toMap()["running"].toBool());
        wall += 877; mono += 877;
        QVERIFY(c.pause(a));
        QCOMPARE(seconds(c, 0), 11);
        QVERIFY(c.saveDraft(a, "Review", "{}", "00:01:15", false));
        QCOMPARE(seconds(c, 0), 75);
        QVERIFY(c.start(b));
    }
    wall += 30000; mono += 30000;
    {
        Capture c(path, w, m, "boot");
        QCOMPARE(seconds(c, 0), 75);
        QCOMPARE(seconds(c, 1), 35);
        QVERIFY(c.pause(b));
        wall += 5000; mono += 5000;
        QCOMPARE(seconds(c, 1), 35);
    }
}

void CaptureTests::clockDiscontinuity()
{
    QTemporaryDir dir;
    qint64 wall = 1700000000000, mono = 100000;
    auto w = [&] { return wall; };
    auto m = [&] { return mono; };
    const QString path = dir.path() + "/state.json";
    QString id;
    {
        Capture c(path, w, m, "boot");
        QVERIFY(c.addDraft());
        id = draftId(c);
        QVERIFY(c.start(id));
        wall -= 60000; mono += 1000;
        QVERIFY(c.pause(id));
        QVERIFY(c.drafts()[0].toMap()["uncertain"].toBool());
        QVERIFY(!c.submit(id));
        QVERIFY(!c.saveDraft(id, "task", "{}", "00:00:01", false));
        QVERIFY(c.saveDraft(id, "task", "{}", "00:00:01", true));
        QVERIFY(!c.drafts()[0].toMap()["uncertain"].toBool());
        QVERIFY(c.start(id));
    }
    wall -= 10000;
    Capture rebooted(path, w, m, "new-boot");
    QVERIFY(rebooted.drafts()[0].toMap()["uncertain"].toBool());
    QVERIFY(!rebooted.drafts()[0].toMap()["running"].toBool());
}

void CaptureTests::rebootRebasesClock()
{
    QTemporaryDir dir;
    qint64 wall = 1700000000000, mono = 100000;
    auto w = [&] { return wall; };
    auto m = [&] { return mono; };
    const QString path = dir.path() + "/state.json";
    {
        Capture c(path, w, m, "old-boot");
        QVERIFY(c.addDraft());
        QVERIFY(c.start(draftId(c)));
    }
    wall += 30000;
    mono = 1000;
    Capture c(path, w, m, "new-boot");
    QCOMPARE(seconds(c, 0), 30);
    wall -= 60000;
    mono += 1000;
    QVERIFY(c.pause(draftId(c)));
    QVERIFY(c.drafts()[0].toMap()["uncertain"].toBool());
    QCOMPARE(seconds(c, 0), 31);
}

void CaptureTests::offlineAndDestinations()
{
    QTemporaryDir dir;
    qint64 wall = 1700000000000, mono = 100000;
    auto w = [&] { return wall; };
    auto m = [&] { return mono; };
    const QString path = dir.path() + "/state.json";
    QString payload, id;
    {
        Capture c(path, w, m, "boot");
        QVERIFY(c.setServiceUrl("http://127.0.0.1:1"));
        QVERIFY(c.addDraft());
        QVERIFY(c.saveDraft(draftId(c), "one", "{\"location\":\"home\",\"n\":1.0}", "00:00:42", false));
        QVERIFY(c.submit(draftId(c)));
        QCOMPARE(c.drafts().size(), 0);
        QCOMPARE(c.pending().size(), 1);
        payload = c.pending()[0].toMap()["payload"].toString();
        id = c.pending()[0].toMap()["id"].toString();
        QVERIFY(c.setServiceUrl("http://127.0.0.1:2"));
        QVERIFY(c.addDraft());
        QVERIFY(c.saveDraft(draftId(c), "two", "{}", "00:00:01", false));
        QVERIFY(c.submit(draftId(c)));
    }
    Capture restored(path, w, m, "boot");
    QCOMPARE(restored.pending().size(), 2);
    QCOMPARE(restored.pending()[0].toMap()["destination"].toString(), "http://127.0.0.1:1");
    QCOMPARE(restored.pending()[1].toMap()["destination"].toString(), "http://127.0.0.1:2");
    QCOMPARE(restored.pending()[0].toMap()["payload"].toString(), payload);
    QCOMPARE(restored.pending()[0].toMap()["id"].toString(), id);
    QVERIFY(payload.contains("\"n\":1.0"));
    const auto request = QJsonDocument::fromJson(payload.toUtf8()).object();
    QVERIFY(request.value("started_at").isNull());
    QCOMPARE(request.value("attributes").toObject().value("location").toString(), "home");
}

void CaptureTests::retriesAndAcknowledgments()
{
    QTemporaryDir dir;
    qint64 wall = 1700000000000, mono = 100000;
    auto w = [&] { return wall; };
    auto m = [&] { return mono; };
    Service service;
    Capture c(dir.path() + "/state.json", w, m, "boot");
    QVERIFY(c.setServiceUrl(service.url()));
    QVERIFY(c.addDraft());
    QVERIFY(c.saveDraft(draftId(c), "task", "{}", "00:00:10", false));
    service.loseResponse = true;
    QVERIFY(c.submit(draftId(c)));
    QTRY_COMPARE(service.requests.size(), 1);
    QTRY_VERIFY(!c.pending()[0].toMap()["message"].toString().isEmpty());
    const QString id = c.pending()[0].toMap()["id"].toString();
    service.statuses << 503 << 429 << 409 << 422;
    QVERIFY(c.retry(id));
    QTRY_COMPARE(service.requests.size(), 2);
    QTRY_VERIFY(c.pending()[0].toMap()["message"].toString().contains("503"));
    QVERIFY(c.retry(id));
    QTRY_COMPARE(service.requests.size(), 3);
    QTRY_VERIFY(c.pending()[0].toMap()["message"].toString().contains("429"));
    QVERIFY(c.retry(id));
    QTRY_COMPARE(service.requests.size(), 4);
    QTRY_COMPARE(c.pending()[0].toMap()["status"].toString(), "error");
    wall += 600000;
    QTest::qWait(1200);
    QCOMPARE(service.requests.size(), 4);
    QVERIFY(c.retry(id));
    QTRY_COMPARE(service.requests.size(), 5);
    QTRY_COMPARE(c.pending()[0].toMap()["status"].toString(), "error");
    QVERIFY(c.pending()[0].toMap()["message"].toString().contains("422"));
    service.mismatch = true;
    service.statuses << 200;
    QVERIFY(c.retry(id));
    QTRY_COMPARE(service.requests.size(), 6);
    QTRY_VERIFY(c.pending()[0].toMap()["message"].toString().contains("mismatched"));
    service.mismatch = false;
    service.deleted = true;
    service.statuses << 200;
    QVERIFY(c.retry(id));
    QTRY_COMPARE(c.pending().size(), 0);
    QCOMPARE(service.requests.size(), 7);
    for (const auto &request : service.requests) QCOMPARE(request, service.requests[0]);
}

void CaptureTests::automaticRetryAndCopy()
{
    QTemporaryDir dir;
    qint64 wall = 1700000000000, mono = 100000;
    Service service;
    service.statuses << 503 << 201;
    Capture c(dir.path() + "/state.json", [&] { return wall; }, [&] { return mono; }, "boot");
    QVERIFY(c.setServiceUrl(service.url()));
    QVERIFY(c.addDraft());
    QVERIFY(c.saveDraft(draftId(c), "task", "{\"value\":1.0}", "00:00:10", false));
    QVERIFY(c.submit(draftId(c)));
    QTRY_VERIFY(!c.pending()[0].toMap()["message"].toString().isEmpty());
    QVERIFY(c.copyPending(c.pending()[0].toMap()["id"].toString()));
    QCOMPARE(c.drafts().size(), 1);
    QCOMPARE(c.pending().size(), 1);
    QCOMPARE(c.drafts()[0].toMap()["attributes_json"].toString(), "{\"value\":1.0}");
    wall += 3000;
    mono += 3000;
    QTRY_COMPARE_WITH_TIMEOUT(c.pending().size(), 0, 4000);
    QCOMPARE(service.requests.size(), 2);
    QCOMPARE(service.requests[0], service.requests[1]);
}

void CaptureTests::rejectsCorruptState()
{
    QTemporaryDir dir;
    const QString path = dir.path() + "/state.json";
    const QByteArray invalid = "{\"version\":1,\"service\":\"http://localhost\","
                               "\"drafts\":[{}],\"pending\":[]}";
    QFile file(path);
    QVERIFY(file.open(QIODevice::WriteOnly));
    QCOMPARE(file.write(invalid), invalid.size());
    file.close();
    Capture c(path, [] { return qint64(10000); }, [] { return qint64(100); }, "boot");
    QVERIFY(!c.ready());
    QVERIFY(!c.error().isEmpty());
    QVERIFY(file.open(QIODevice::ReadOnly));
    QCOMPARE(file.readAll(), invalid);
}

void CaptureTests::persistenceFailure()
{
    QTemporaryDir dir;
    qint64 wall = 1700000000000, mono = 100000;
    Capture c(dir.path() + "/state.json", [&] { return wall; }, [&] { return mono; }, "boot");
    QVERIFY(c.addDraft());
    QVERIFY(c.saveDraft(draftId(c), "task", "{}", "00:00:01", false));
    QVERIFY(QFile::setPermissions(dir.path(), QFileDevice::ReadOwner | QFileDevice::ExeOwner));
    QVERIFY(!c.submit(draftId(c)));
    QCOMPARE(c.drafts().size(), 1);
    QCOMPARE(c.pending().size(), 0);
    QVERIFY(!c.error().isEmpty());
    QVERIFY(QFile::setPermissions(dir.path(), QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner));
}

void CaptureTests::liveServerIntegration()
{
    const QString url = qEnvironmentVariable("TIMETRACKER_TEST_SERVER");
    if (url.isEmpty()) QSKIP("Set TIMETRACKER_TEST_SERVER for a live-server integration test.");
    QTemporaryDir dir;
    qint64 wall = 1700000000000, mono = 100000;
    Capture c(dir.path() + "/state.json", [&] { return wall; }, [&] { return mono; }, "boot");
    const QString task = QStringLiteral("Plasma integration ") + QUuid::createUuid().toString(QUuid::WithoutBraces);
    QVERIFY(c.setServiceUrl(url));
    QVERIFY(c.addDraft());
    QVERIFY(c.saveDraft(draftId(c), task, "{\"value\":1.0,\"flag\":true}", "00:01:15", false));
    QVERIFY(c.submit(draftId(c)));
    const QString id = c.pending()[0].toMap()["id"].toString();
    QTRY_COMPARE_WITH_TIMEOUT(c.pending().size(), 0, 10000);
    QNetworkAccessManager network;
    auto *reply = network.get(QNetworkRequest(QUrl(url + "/v0/entries/" + id)));
    QSignalSpy completed(reply, &QNetworkReply::finished);
    if (!reply->isFinished()) QVERIFY(completed.wait(10000));
    QCOMPARE(reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt(), 200);
    const QByteArray body = reply->readAll();
    QVERIFY(body.contains("\"value\":1.0"));
    const auto entry = QJsonDocument::fromJson(body).object();
    QCOMPARE(entry.value("task").toString(), task);
    QCOMPARE(entry.value("duration_seconds").toInt(), 75);
    QVERIFY(entry.value("started_at").isNull());
    reply->deleteLater();
    c.completeTask(task);
    QTRY_VERIFY_WITH_TIMEOUT(c.suggestions().contains(task), 10000);
}

void CaptureTests::pluginInstalledAfterFailedLoad()
{
    QTemporaryDir dir;
    QVERIFY(dir.isValid());
    QVERIFY(QDir().mkpath(dir.path() + "/backend"));
    QFile module(dir.path() + "/backend/qmldir");
    QVERIFY(module.open(QIODevice::WriteOnly));
    QCOMPARE(module.write("plugin timetrackerplugin\n"), qint64(25));
    module.close();
    const QByteArray qml = "import QtQml 2.15\nimport \"backend\" 1.0 as Backend\nBackend.Capture {}\n";
    const QUrl base = QUrl::fromLocalFile(dir.path() + "/main.qml");
    QQmlEngine engine;
    QQmlComponent first(&engine);
    first.setData(qml, base);
    QVERIFY(first.isError());
    QVERIFY(first.errorString().contains("plugin \"timetrackerplugin\" not found"));
    const QString builtPlugin = QCoreApplication::applicationDirPath()
        + "/package/contents/ui/backend/libtimetrackerplugin.so";
    QVERIFY2(QFile::copy(builtPlugin, dir.path() + "/backend/libtimetrackerplugin.so"),
             qPrintable(builtPlugin));
    QQmlComponent second(&engine);
    second.setData(qml, base);
    QVERIFY(second.isError());
    QVERIFY(second.errorString().contains("plugin \"timetrackerplugin\" not found"));
    engine.clearComponentCache();
    QQmlComponent cleared(&engine);
    cleared.setData(qml, base);
    QVERIFY2(cleared.isReady(), qPrintable(cleared.errorString()));
    std::unique_ptr<QObject> clearedObject(cleared.create());
    QVERIFY2(clearedObject != nullptr, qPrintable(cleared.errorString()));
    QQmlEngine freshEngine;
    QQmlComponent fresh(&freshEngine);
    fresh.setData(qml, base);
    QVERIFY2(fresh.isReady(), qPrintable(fresh.errorString()));
    std::unique_ptr<QObject> object(fresh.create());
    QVERIFY2(object != nullptr, qPrintable(fresh.errorString()));
}

QTEST_GUILESS_MAIN(CaptureTests)
#include "capture_tests.moc"
