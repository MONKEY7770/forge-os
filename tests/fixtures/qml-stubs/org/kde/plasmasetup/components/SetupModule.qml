import QtQuick
// Stand-in matching componentsplugin.qmltypes from Plasma Setup 6.8.3 (QObject, required contentItem).
QtObject {
    property bool available: true
    property real cardWidth: 400
    property bool nextEnabled: true
    required property Item contentItem
    default property list<QtObject> children
    signal nextRequested()
}
