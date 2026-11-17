#include "capture.h"

#include <QQmlExtensionPlugin>
#include <qqml.h>

class TimeTrackerPlugin : public QQmlExtensionPlugin {
    Q_OBJECT
    Q_PLUGIN_METADATA(IID QQmlExtensionInterface_iid)

public:
    void registerTypes(const char *uri) override
    {
        qmlRegisterType<Capture>(uri, 1, 0, "Capture");
    }
};

#include "plugin.moc"
