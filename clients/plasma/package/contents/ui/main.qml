import QtQuick 2.15
import QtQuick.Layouts 1.15
import QtQuick.Controls 2.15 as Controls
import org.kde.plasma.plasmoid 2.0
import org.kde.plasma.core 2.0 as PlasmaCore
import "backend" 1.0 as Backend

Item {
    id: root
    width: PlasmaCore.Units.gridUnit * 26
    height: PlasmaCore.Units.gridUnit * 30
    property string selectedId: ""
    property var selected: null
    property var editor: null
    property bool dirty: false
    property bool loadingFields: false

    function formatDuration(seconds) {
        var h = Math.floor(seconds / 3600)
        var m = Math.floor(seconds / 60) % 60
        var s = seconds % 60
        return (h < 10 ? "0" : "") + h + ":" +
               (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s
    }

    function refreshSelection(loadFields) {
        var wasRunning = selected && selected.running
        var found = null
        for (var i = 0; i < capture.drafts.length; ++i) {
            if (capture.drafts[i].id === selectedId) {
                found = capture.drafts[i]
                break
            }
        }
        selected = found
        if (wasRunning && found && !found.running)
            loadFields = true
        if (loadFields && found && editor) {
            loadingFields = true
            editor.taskField.text = found.task
            editor.metadataField.text = found.attributes_json
            editor.durationField.text = formatDuration(found.display_seconds)
            editor.reviewedField.checked = false
            dirty = false
            loadingFields = false
        } else if (found && found.running && editor) {
            editor.durationField.text = formatDuration(found.display_seconds)
        }
    }

    function selectDraft(id) {
        if (selectedId !== id && dirty && !save())
            return false
        selectedId = id
        refreshSelection(true)
        return true
    }

    function save() {
        if (!selected || !editor || !capture.saveDraft(selectedId, editor.taskField.text,
                editor.metadataField.text, editor.durationField.text, editor.reviewedField.checked))
            return false
        dirty = false
        return true
    }

    Backend.Capture {
        id: capture
        Component.onCompleted: {
            if (initialize(String(plasmoid.id)) && drafts.length > 0)
                root.selectDraft(drafts[0].id)
        }
        onChanged: root.refreshSelection(false)
    }

    Plasmoid.icon: "chronometer"
    Plasmoid.toolTipMainText: "Time Tracker"
    Plasmoid.toolTipSubText: capture.pending.length + " pending submissions"
    Plasmoid.preferredRepresentation: Plasmoid.compactRepresentation

    Plasmoid.fullRepresentation: Controls.ScrollView {
        id: full
        property alias taskField: task
        property alias metadataField: metadata
        property alias durationField: duration
        property alias reviewedField: reviewed
        Component.onCompleted: {
            root.editor = full
            root.refreshSelection(true)
        }
        Component.onDestruction: root.editor = null
        Layout.minimumWidth: PlasmaCore.Units.gridUnit * 24
        Layout.minimumHeight: PlasmaCore.Units.gridUnit * 22
        Layout.preferredWidth: PlasmaCore.Units.gridUnit * 28
        Layout.preferredHeight: PlasmaCore.Units.gridUnit * 32
        clip: true
        contentWidth: availableWidth

        ColumnLayout {
            width: parent.width
            spacing: 8

            Controls.Label {
                text: "Time Tracker"
                font.bold: true
                font.pointSize: 14
            }

            Controls.Label {
                visible: capture.error.length > 0
                text: capture.error
                color: "#c33d3d"
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            Controls.Label {
                visible: root.dirty
                text: "Unsaved edits"
            }
            Controls.Button {
                visible: capture.error.length > 0
                text: "Dismiss message"
                onClicked: capture.clearError()
            }

            Controls.Label { text: "Service URL" }
            RowLayout {
                Layout.fillWidth: true
                Controls.TextField {
                    id: service
                    Layout.fillWidth: true
                    text: capture.serviceUrl
                    placeholderText: "http://host:8000"
                    selectByMouse: true
                    Accessible.name: "Service URL"
                }
                Controls.Button {
                    text: "Set"
                    enabled: capture.ready
                    onClicked: capture.setServiceUrl(service.text)
                }
            }

            Controls.Label { text: "Drafts"; font.bold: true }
            Repeater {
                model: capture.drafts
                delegate: Controls.Button {
                    Layout.fillWidth: true
                    text: (modelData.running ? "▶ " : "") +
                          (modelData.task.length ? modelData.task : "(Untitled)") +
                          "  " + root.formatDuration(modelData.display_seconds) +
                          (modelData.uncertain ? " — review" : "")
                    highlighted: root.selectedId === modelData.id
                    onClicked: root.selectDraft(modelData.id)
                }
            }
            Controls.Button {
                text: "New draft"
                enabled: capture.ready
                onClicked: {
                    if (root.dirty && !root.save())
                        return
                    if (capture.addDraft())
                        root.selectDraft(capture.drafts[capture.drafts.length - 1].id)
                }
            }

            Controls.Label {
                visible: !root.selected
                text: "Select a draft or create one."
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            ColumnLayout {
                visible: !!root.selected
                enabled: capture.ready
                Layout.fillWidth: true

                Controls.Label { text: "Task" }
                Controls.TextField {
                    id: task
                    Layout.fillWidth: true
                    enabled: root.selected && !root.selected.running
                    placeholderText: "What are you working on?"
                    selectByMouse: true
                    Accessible.name: "Task"
                    onTextEdited: {
                        root.dirty = true
                        completionDelay.restart()
                        autosave.restart()
                    }
                }
                Timer {
                    id: completionDelay
                    interval: 400
                    onTriggered: capture.completeTask(task.text)
                }
                Controls.ComboBox {
                    Layout.fillWidth: true
                    visible: capture.suggestions.length > 0 && root.selected && !root.selected.running
                    model: capture.suggestions
                    displayText: "Choose a suggestion"
                    onActivated: {
                        task.text = capture.suggestions[index]
                        root.dirty = true
                        autosave.restart()
                        task.forceActiveFocus()
                    }
                }

                Controls.Label { text: "Metadata (JSON object)" }
                Controls.TextArea {
                    id: metadata
                    Layout.fillWidth: true
                    Layout.preferredHeight: 65
                    enabled: root.selected && !root.selected.running
                    selectByMouse: true
                    wrapMode: TextEdit.Wrap
                    Accessible.name: "Metadata JSON"
                    onTextChanged: {
                        if (!root.loadingFields) {
                            root.dirty = true
                            autosave.restart()
                        }
                    }
                }
                Controls.Label { text: "Duration (HH:MM:SS)" }
                Controls.TextField {
                    id: duration
                    Layout.fillWidth: true
                    enabled: root.selected && !root.selected.running
                    selectByMouse: true
                    Accessible.name: "Duration"
                    onTextEdited: {
                        root.dirty = true
                        autosave.restart()
                    }
                }
                Controls.CheckBox {
                    id: reviewed
                    visible: root.selected && root.selected.uncertain
                    text: "I reviewed and corrected this duration"
                    Layout.fillWidth: true
                }
                RowLayout {
                    Controls.Button {
                        text: root.selected && root.selected.running ? "Pause" : "Start"
                        onClicked: {
                            if (root.selected.running) {
                                if (capture.pause(root.selectedId)) root.refreshSelection(true)
                            } else if (root.save() && capture.start(root.selectedId)) {
                                root.refreshSelection(true)
                            }
                        }
                    }
                    Controls.Button {
                        text: "Save"
                        enabled: root.selected && !root.selected.running
                        onClicked: { if (root.save()) root.refreshSelection(true) }
                    }
                    Controls.Button {
                        text: "Submit"
                        enabled: root.selected && !root.selected.running
                        onClicked: {
                            if (root.save() && capture.submit(root.selectedId)) {
                                root.selectedId = ""
                                root.refreshSelection(false)
                            }
                        }
                    }
                    Controls.Button {
                        text: "Delete"
                        onClicked: deleteDraftDialog.open()
                    }
                }
            }

            Controls.Label { text: "Pending submissions"; font.bold: true }
            Controls.Label {
                visible: capture.pending.length === 0
                text: "None"
            }
            Repeater {
                model: capture.pending
                delegate: ColumnLayout {
                    Layout.fillWidth: true
                    Controls.Label {
                        Layout.fillWidth: true
                        wrapMode: Text.Wrap
                        text: modelData.task + " — " + modelData.status +
                              (modelData.message ? ": " + modelData.message : "") +
                              "\n" + modelData.destination + "\n" + modelData.id
                    }
                    RowLayout {
                        Controls.Button {
                            text: "Retry now"
                            onClicked: capture.retry(modelData.id)
                        }
                        Controls.Button {
                            text: "Copy to draft"
                            onClicked: {
                                copyDialog.pendingId = modelData.id
                                copyDialog.open()
                            }
                        }
                        Controls.Button {
                            text: "Discard"
                            onClicked: {
                                discardDialog.pendingId = modelData.id
                                discardDialog.open()
                            }
                        }
                    }
                }
            }
        }
    }

    Timer {
        id: autosave
        interval: 1000
        onTriggered: {
            if (root.dirty && root.selected && !root.selected.running)
                root.save()
        }
    }

    Controls.Dialog {
        id: copyDialog
        property string pendingId: ""
        title: "Copy this request?"
        modal: true
        standardButtons: Controls.Dialog.Yes | Controls.Dialog.No
        Controls.Label {
            text: "The original remains queued and may still be delivered. Submitting the copy creates a new entry."
            wrapMode: Text.Wrap
            width: PlasmaCore.Units.gridUnit * 20
        }
        onAccepted: {
            if (root.dirty && !root.save())
                return
            if (capture.copyPending(pendingId))
                root.selectDraft(capture.drafts[capture.drafts.length - 1].id)
        }
    }
    Controls.Dialog {
        id: deleteDraftDialog
        title: "Delete this draft?"
        modal: true
        standardButtons: Controls.Dialog.Yes | Controls.Dialog.No
        onAccepted: {
            if (capture.removeDraft(root.selectedId)) {
                root.selectedId = ""
                root.refreshSelection(false)
            }
        }
    }
    Controls.Dialog {
        id: discardDialog
        property string pendingId: ""
        title: "Discard this unsent request?"
        modal: true
        standardButtons: Controls.Dialog.Yes | Controls.Dialog.No
        onAccepted: capture.discardPending(pendingId)
    }
}
