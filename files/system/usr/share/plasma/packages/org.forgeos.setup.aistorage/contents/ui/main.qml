// SPDX-License-Identifier: Apache-2.0
// Forge OS: first-boot wizard page (Plasma Setup, after the account page) for the AI model storage limit.
//
// Not yet tried in a real wizard. Written against Plasma Setup 6.8.3 as installed in this image
// (checked 11-24: org.kde.plasmasetup.components exports SetupModule, a QObject with "available",
// "nextEnabled", "cardWidth", a required "contentItem" and a nextRequested signal; the wizard calls
// onPageActivated(); the layout follows the built-in account page).
//
// The wizard runs as the plasma-setup system user, so this page cannot change Forge Settings itself:
//  - it reads the disk size from /run/forge/disk.ini (written by forge-model-guard, as root, every 30 s
//    from boot). Without that file it still shows the rule, only without numbers;
//  - it saves the choice to /var/lib/forge-setup/storage.ini (only that user and root may enter the
//    folder). forge-setup-storage.service checks that file strictly, saves the choice in Forge Settings
//    with forge-apply and removes it.
// QML can read and write INI files (QtCore Settings) but not JSON files, hence INI.
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import QtCore
import org.kde.kirigami as Kirigami
import org.kde.plasmasetup.components as PlasmaSetupComponents

PlasmaSetupComponents.SetupModule {
    id: root

    available: true
    nextEnabled: true

    // Thai when the wizard runs in Thai, else English (the same words as Forge Settings).
    readonly property bool thai: Qt.locale().name.indexOf("th") === 0
    function tr(en, th) { return thai ? th : en }

    // The same defaults as Forge Settings (ai.storage_reserve_*): keep 30%, at least 128 GB, at most 600 GB.
    property string mode: "auto"
    property int pct: 30
    property int gb: autoGb > 0 ? autoGb : 300

    Settings {
        id: diskInfo
        location: "file:///run/forge/disk.ini"
        category: "disk"
    }
    // Settings.value() is not a binding, so the numbers are read again until the guard has written
    // them (it first runs about 15 s after boot; the wizard may start sooner).
    property real totalGb: 0
    property real autoGb: 0
    function readDisk() {
        diskInfo.sync()
        root.totalGb = Number(diskInfo.value("total_gb", 0)) || 0
        root.autoGb = Number(diskInfo.value("auto_reserve_gb", 0)) || 0
    }
    Component.onCompleted: readDisk()
    function onPageActivated() { readDisk() }
    Timer {
        interval: 3000
        repeat: true
        running: root.totalGb <= 0
        onTriggered: root.readDisk()
    }

    Settings {
        id: choice
        location: "file:///var/lib/forge-setup/storage.ini"
        category: "storage"
    }

    // GB kept for games, apps and files on this disk, or -1 when the disk size is not known.
    function reserveGb() {
        if (totalGb <= 0)
            return -1
        let r
        if (mode === "percent")
            r = totalGb * pct / 100
        else if (mode === "gb")
            r = gb
        else
            r = Math.min(Math.max(Math.floor(totalGb * 0.3 / 10 + 0.5) * 10, 128), 600)
        return Math.min(r, totalGb)
    }
    readonly property real previewReserve: reserveGb()

    // The wizard sends nextRequested when the user presses Next: save the choice (only then, so moving
    // the slider does not save every step).
    onNextRequested: {
        choice.setValue("mode", root.mode)
        if (root.mode === "percent")
            choice.setValue("pct", root.pct)
        if (root.mode === "gb")
            choice.setValue("gb", root.gb)
        choice.sync()
    }

    contentItem: QQC2.ScrollView {
        id: scroll
        clip: true

        ColumnLayout {
            width: scroll.availableWidth
            height: Math.max(implicitHeight, scroll.availableHeight)

            Item { Layout.fillHeight: true }

            ColumnLayout {
                Layout.alignment: Qt.AlignHCenter
                Layout.fillWidth: true
                Layout.maximumWidth: root.cardWidth > 0 ? root.cardWidth : Kirigami.Units.gridUnit * 30
                spacing: Kirigami.Units.largeSpacing

                Kirigami.Heading {
                    Layout.fillWidth: true
                    level: 2
                    wrapMode: Text.WordWrap
                    text: root.tr("Space for games and AI models", "พื้นที่สำหรับเกมและโมเดล AI")
                }

                QQC2.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: root.tr("AI models can be very large. Forge OS keeps part of the disk for games, apps and files, "
                          + "and lets AI models use only the rest. You can change this later in Forge Settings.",
                          "โมเดล AI อาจมีขนาดใหญ่มาก Forge OS จะกันพื้นที่ดิสก์ส่วนหนึ่งไว้สำหรับเกม แอป และไฟล์ "
                          + "แล้วให้โมเดล AI ใช้ได้เฉพาะส่วนที่เหลือ คุณเปลี่ยนค่านี้ภายหลังได้ใน Forge Settings")
                }

                QQC2.Label {
                    visible: root.totalGb > 0
                    text: root.tr("This disk: %1 GB", "ดิสก์นี้: %1 GB").arg(Math.round(root.totalGb))
                }

                QQC2.RadioButton {
                    Layout.fillWidth: true
                    checked: root.mode === "auto"
                    text: root.tr("Automatic (recommended): 30% of the disk, at least 128 GB and at most 600 GB",
                          "อัตโนมัติ (แนะนำ): 30% ของดิสก์ อย่างน้อย 128 GB และไม่เกิน 600 GB")
                    onToggled: if (checked) root.mode = "auto"
                }

                QQC2.RadioButton {
                    Layout.fillWidth: true
                    checked: root.mode === "percent"
                    text: root.tr("A share of the disk", "เป็นเปอร์เซ็นต์ของดิสก์")
                    onToggled: if (checked) root.mode = "percent"
                }

                RowLayout {
                    visible: root.mode === "percent"
                    Layout.fillWidth: true
                    QQC2.Slider {
                        Layout.fillWidth: true
                        from: 0
                        to: 95
                        stepSize: 5
                        value: root.pct
                        onMoved: root.pct = Math.round(value)
                    }
                    QQC2.Label {
                        text: root.pct + "%"
                    }
                }

                QQC2.RadioButton {
                    Layout.fillWidth: true
                    checked: root.mode === "gb"
                    text: root.tr("A fixed size (GB)", "กำหนดขนาดเอง (GB)")
                    onToggled: if (checked) root.mode = "gb"
                }

                RowLayout {
                    visible: root.mode === "gb"
                    QQC2.SpinBox {
                        from: 0
                        to: 100000
                        stepSize: 10
                        editable: true
                        value: root.gb
                        onValueModified: root.gb = value
                    }
                    QQC2.Label {
                        text: "GB"
                    }
                }

                Kirigami.Separator {
                    Layout.fillWidth: true
                }

                QQC2.Label {
                    visible: root.previewReserve >= 0
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    font.bold: true
                    text: root.tr("Kept for games, apps and files: %1 GB", "กันไว้สำหรับเกม แอป และไฟล์: %1 GB")
                        .arg(Math.round(root.previewReserve))
                }

                QQC2.Label {
                    visible: root.previewReserve >= 0
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: root.tr("AI models may use up to: %1 GB", "โมเดล AI ใช้ได้สูงสุด: %1 GB")
                        .arg(Math.round(root.totalGb - root.previewReserve))
                }

                QQC2.Label {
                    visible: root.previewReserve < 0
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: root.tr("The disk size is not known yet, so there is no preview. The rule you pick is used for "
                          + "the disk that holds the AI models.",
                          "ยังไม่ทราบขนาดดิสก์ จึงแสดงตัวอย่างไม่ได้ กฎที่คุณเลือกจะใช้กับดิสก์ที่เก็บโมเดล AI")
                }

                QQC2.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    opacity: 0.7
                    text: root.tr("Another 30 GB always stays free for system updates.",
                          "และจะเว้นพื้นที่ว่างไว้อีก 30 GB เสมอสำหรับอัปเดตระบบ")
                }
            }

            Item { Layout.fillHeight: true }
        }
    }
}
