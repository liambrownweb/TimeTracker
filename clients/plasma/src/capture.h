#pragma once

#include <QJsonArray>
#include <QJsonObject>
#include <QLockFile>
#include <QNetworkAccessManager>
#include <QObject>
#include <QTimer>
#include <functional>
#include <memory>

class QNetworkReply;

class Capture : public QObject {
    Q_OBJECT
    Q_PROPERTY(QVariantList drafts READ drafts NOTIFY changed)
    Q_PROPERTY(QVariantList pending READ pending NOTIFY changed)
    Q_PROPERTY(QString serviceUrl READ serviceUrl NOTIFY changed)
    Q_PROPERTY(QString error READ error NOTIFY errorChanged)
    Q_PROPERTY(QStringList suggestions READ suggestions NOTIFY suggestionsChanged)
    Q_PROPERTY(bool ready READ ready NOTIFY changed)

public:
    explicit Capture(QObject *parent = nullptr);
    Capture(const QString &path, std::function<qint64()> wall,
            std::function<qint64()> monotonic, const QString &bootId,
            QObject *parent = nullptr);

    QVariantList drafts() const;
    QVariantList pending() const;
    QString serviceUrl() const;
    QString error() const { return m_error; }
    QStringList suggestions() const { return m_suggestions; }
    bool ready() const { return m_ready; }

    Q_INVOKABLE bool initialize(const QString &instance);
    Q_INVOKABLE bool setServiceUrl(const QString &url);
    Q_INVOKABLE bool addDraft();
    Q_INVOKABLE bool saveDraft(const QString &id, const QString &task,
                               const QString &attributes, const QString &duration,
                               bool reviewed);
    Q_INVOKABLE bool start(const QString &id);
    Q_INVOKABLE bool pause(const QString &id);
    Q_INVOKABLE bool submit(const QString &id);
    Q_INVOKABLE bool removeDraft(const QString &id);
    Q_INVOKABLE bool retry(const QString &id);
    Q_INVOKABLE bool copyPending(const QString &id);
    Q_INVOKABLE bool discardPending(const QString &id);
    Q_INVOKABLE void completeTask(const QString &prefix);
    Q_INVOKABLE void clearError();

signals:
    void changed();
    void errorChanged();
    void suggestionsChanged();

private:
    bool open(const QString &path);
    bool persist(const QJsonObject &next);
    bool fail(const QString &message);
    int findDraft(const QString &id) const;
    int findPending(const QString &id) const;
    bool settle(QJsonObject &draft, qint64 wall, qint64 mono);
    void tick();
    void deliver();
    void finished(const QString &id, QNetworkReply *reply);
    qint64 elapsed(const QJsonObject &draft, qint64 wall, qint64 mono, bool *uncertain) const;
    static bool validUrl(const QString &url);
    static QUrl endpoint(const QString &base, const QString &path);

    QJsonObject m_state;
    QString m_path;
    QString m_error;
    QString m_bootId;
    QStringList m_suggestions;
    std::function<qint64()> m_wall;
    std::function<qint64()> m_mono;
    std::unique_ptr<QLockFile> m_lock;
    QNetworkAccessManager m_network;
    QTimer m_timer;
    bool m_ready = false;
    bool m_inFlight = false;
    quint64 m_completionGeneration = 0;
};
