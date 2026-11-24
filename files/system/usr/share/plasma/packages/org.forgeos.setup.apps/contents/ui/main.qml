// SPDX-License-Identifier: Apache-2.0
// Forge OS: first-boot wizard page (Plasma Setup, after the AI storage page) to choose the optional apps.
//
// Written like the AI storage page (Plasma Setup 6.8.3 as installed in this image: SetupModule is a
// QObject with "available", "nextEnabled", "cardWidth", a required "contentItem" and a nextRequested
// signal; the wizard calls onPageActivated()).
//
// The wizard runs as the plasma-setup system user, so this page cannot install anything itself. When
// you press Next it saves one line per app (true or false) to /var/lib/forge-setup/apps.ini (only that
// user and root may enter the folder). forge-setup-apps.service checks the file strictly, saves the
// choice as Forge Settings switches (Apps) and removes it; forge-apps then installs what is ticked.
// Nothing is downloaded before this choice; built-in apps are hidden instead of deleted. The list below must match
// /usr/share/forge/apps.json (a test checks it).
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

    readonly property bool thai: Qt.locale().name.indexOf("th") === 0
    function tr(en, th) { return thai ? th : en }

    // id: the name in apps.ini; on: ticked at first (the Forge Settings default).
    readonly property var apps: [
        { id: "firefox", section: "essentials", on: true, en: "Firefox (web browser)", th: "Firefox (เว็บเบราว์เซอร์)", noteEn: "Without it there is no web browser until you install one.", noteTh: "หากไม่เลือก จะไม่มีเว็บเบราว์เซอร์จนกว่าคุณจะติดตั้งเอง" },
        { id: "flatseal", section: "essentials", on: true, en: "Flatseal (what each app may access)", th: "Flatseal (สิทธิ์การเข้าถึงของแต่ละแอป)", noteEn: "", noteTh: "" },
        { id: "missioncenter", section: "essentials", on: true, en: "Mission Center (CPU, GPU and memory per app)", th: "Mission Center (CPU, GPU และหน่วยความจำของแต่ละแอป)", noteEn: "", noteTh: "" },
        { id: "discord", section: "gaming", on: true, en: "Discord", th: "Discord", noteEn: "", noteTh: "" },
        { id: "sober", section: "gaming", on: true, en: "Roblox (Sober)", th: "Roblox (Sober)", noteEn: "", noteTh: "" },
        { id: "vinegar", section: "gaming", on: true, en: "Roblox Studio (Vinegar)", th: "Roblox Studio (Vinegar)", noteEn: "", noteTh: "" },
        { id: "prism", section: "gaming", on: true, en: "Minecraft Java (Prism Launcher)", th: "Minecraft Java (Prism Launcher)", noteEn: "", noteTh: "" },
        { id: "mcpelauncher", section: "gaming", on: true, en: "Minecraft Bedrock", th: "Minecraft Bedrock", noteEn: "", noteTh: "" },
        { id: "moonlight", section: "gaming", on: true, en: "Moonlight (play games from another PC)", th: "Moonlight (เล่นเกมจากพีซีเครื่องอื่น)", noteEn: "", noteTh: "" },
        { id: "gpu_recorder", section: "gaming", on: true, en: "GPU Screen Recorder", th: "GPU Screen Recorder", noteEn: "", noteTh: "" },
        { id: "retrodeck", section: "gaming", on: false, en: "Retro gaming (RetroDECK)", th: "เกมย้อนยุค (RetroDECK)", noteEn: "You add your own games (ROMs).", noteTh: "คุณเพิ่มเกม (ROM) ของคุณเอง" },
        { id: "android", section: "gaming", on: false, en: "Android apps (Waydroid)", th: "แอป Android (Waydroid)", noteEn: "Downloads about 1 GB. Some apps and games won't run, more so on NVIDIA.", noteTh: "ดาวน์โหลดประมาณ 1 GB แอปและเกมบางตัวใช้งานไม่ได้ โดยเฉพาะบนการ์ดจอ NVIDIA" },
        { id: "vms", section: "gaming", on: false, en: "Virtual machines (Windows gaming ready)", th: "เครื่องเสมือน (พร้อมเล่นเกม Windows)", noteEn: "Needs a restart. Many online games with anti-cheat refuse to run in a VM.", noteTh: "ต้องรีสตาร์ต เกมออนไลน์หลายเกมที่มีระบบกันโกงจะไม่ยอมเล่นในเครื่องเสมือน" },
        { id: "steam", section: "gaming", on: true, en: "Steam (built in)", th: "Steam (มีในระบบ)", noteEn: "Unticked: hidden (it is part of the system).", noteTh: "ไม่เลือก: ซ่อนไว้ (เป็นส่วนหนึ่งของระบบ)" },
        { id: "lutris", section: "gaming", on: true, en: "Lutris (built in)", th: "Lutris (มีในระบบ)", noteEn: "Unticked: hidden (it is part of the system).", noteTh: "ไม่เลือก: ซ่อนไว้ (เป็นส่วนหนึ่งของระบบ)" },
        { id: "input_remapper", section: "gaming", on: true, en: "Input Remapper (built in)", th: "Input Remapper (มีในระบบ)", noteEn: "Unticked: hidden (it is part of the system).", noteTh: "ไม่เลือก: ซ่อนไว้ (เป็นส่วนหนึ่งของระบบ)" },
        { id: "blender", section: "create", on: true, en: "Blender", th: "Blender", noteEn: "", noteTh: "" },
        { id: "blockbench", section: "create", on: true, en: "Blockbench", th: "Blockbench", noteEn: "", noteTh: "" },
        { id: "krita", section: "create", on: true, en: "Krita", th: "Krita", noteEn: "", noteTh: "" },
        { id: "audacity", section: "create", on: true, en: "Audacity", th: "Audacity", noteEn: "", noteTh: "" },
        { id: "pixelorama", section: "create", on: true, en: "Pixelorama", th: "Pixelorama", noteEn: "", noteTh: "" },
        { id: "kdenlive", section: "create", on: true, en: "Kdenlive (video editing)", th: "Kdenlive (ตัดต่อวิดีโอ)", noteEn: "", noteTh: "" },
        { id: "obs", section: "create", on: true, en: "OBS Studio (recording and streaming)", th: "OBS Studio (บันทึกหน้าจอและสตรีม)", noteEn: "", noteTh: "" },
        { id: "vscode", section: "work", on: true, en: "Visual Studio Code", th: "Visual Studio Code", noteEn: "", noteTh: "" },
        { id: "github_desktop", section: "work", on: true, en: "GitHub Desktop", th: "GitHub Desktop", noteEn: "", noteTh: "" },
        { id: "vlc", section: "work", on: true, en: "VLC (videos and music)", th: "VLC (วิดีโอและเพลง)", noteEn: "", noteTh: "" },
        { id: "localsend", section: "work", on: true, en: "LocalSend (send files to your phone)", th: "LocalSend (ส่งไฟล์ไปยังโทรศัพท์)", noteEn: "", noteTh: "" },
        { id: "bitwarden", section: "work", on: true, en: "Bitwarden (passwords)", th: "Bitwarden (รหัสผ่าน)", noteEn: "", noteTh: "" },
        { id: "rustdesk", section: "work", on: true, en: "RustDesk (remote desktop)", th: "RustDesk (ควบคุมเครื่องจากระยะไกล)", noteEn: "", noteTh: "" },
        { id: "claude", section: "work", on: true, en: "Claude (built in)", th: "Claude (มีในระบบ)", noteEn: "Unticked: hidden (it is part of the system).", noteTh: "ไม่เลือก: ซ่อนไว้ (เป็นส่วนหนึ่งของระบบ)" },
        { id: "kate", section: "work", on: true, en: "Kate (code and text editor, built in)", th: "Kate (แก้ไขโค้ดและข้อความ มีในระบบ)", noteEn: "Unticked: hidden (it is part of the system).", noteTh: "ไม่เลือก: ซ่อนไว้ (เป็นส่วนหนึ่งของระบบ)" },
        { id: "kwrite", section: "work", on: true, en: "KWrite (simple text editor, built in)", th: "KWrite (แก้ไขข้อความแบบง่าย มีในระบบ)", noteEn: "Unticked: hidden (it is part of the system).", noteTh: "ไม่เลือก: ซ่อนไว้ (เป็นส่วนหนึ่งของระบบ)" },
        { id: "spectacle", section: "work", on: true, en: "Spectacle (screenshots, built in)", th: "Spectacle (จับภาพหน้าจอ มีในระบบ)", noteEn: "Unticked: hidden (it is part of the system).", noteTh: "ไม่เลือก: ซ่อนไว้ (เป็นส่วนหนึ่งของระบบ)" },
        { id: "filelight", section: "work", on: true, en: "Filelight (what fills your disk, built in)", th: "Filelight (ดูว่าอะไรกินพื้นที่ดิสก์ มีในระบบ)", noteEn: "Unticked: hidden (it is part of the system).", noteTh: "ไม่เลือก: ซ่อนไว้ (เป็นส่วนหนึ่งของระบบ)" },
        { id: "kfind", section: "work", on: true, en: "KFind (find files, built in)", th: "KFind (ค้นหาไฟล์ มีในระบบ)", noteEn: "Unticked: hidden (it is part of the system).", noteTh: "ไม่เลือก: ซ่อนไว้ (เป็นส่วนหนึ่งของระบบ)" },
        { id: "krdc", section: "work", on: true, en: "KRDC (connect to other PCs, built in)", th: "KRDC (เชื่อมต่อพีซีเครื่องอื่น มีในระบบ)", noteEn: "Unticked: hidden (it is part of the system).", noteTh: "ไม่เลือก: ซ่อนไว้ (เป็นส่วนหนึ่งของระบบ)" },
        { id: "krfb", section: "work", on: true, en: "Krfb (share this screen, built in)", th: "Krfb (แชร์หน้าจอนี้ มีในระบบ)", noteEn: "Unticked: hidden (it is part of the system).", noteTh: "ไม่เลือก: ซ่อนไว้ (เป็นส่วนหนึ่งของระบบ)" },
        { id: "webapps", section: "work", on: true, en: "Web Apps (websites as apps, built in)", th: "Web Apps (ใช้เว็บไซต์เป็นแอป มีในระบบ)", noteEn: "Unticked: hidden (it is part of the system).", noteTh: "ไม่เลือก: ซ่อนไว้ (เป็นส่วนหนึ่งของระบบ)" }
    ]
    readonly property var sections: [
        { id: "essentials", en: "Essentials", th: "พื้นฐาน" },
        { id: "gaming", en: "Gaming", th: "เกม" },
        { id: "create", en: "Creating", th: "สร้างสรรค์" },
        { id: "work", en: "Work and tools", th: "งานและเครื่องมือ" }
    ]

    // The current ticks, by id (a fresh object on each change, so bindings update).
    property var ticked: ({})
    Component.onCompleted: {
        let t = {}
        for (let a of apps)
            t[a.id] = a.on
        ticked = t
    }
    function setTick(id, on) {
        let t = Object.assign({}, ticked)
        t[id] = on
        ticked = t
    }
    function countTicked() {
        let n = 0
        for (let a of apps)
            if (ticked[a.id])
                n++
        return n
    }

    Settings {
        id: choice
        location: "file:///var/lib/forge-setup/apps.ini"
        category: "apps"
    }

    onNextRequested: {
        for (let a of root.apps)
            choice.setValue(a.id, root.ticked[a.id] === true)
        choice.sync()
    }

    contentItem: QQC2.ScrollView {
        id: scroll
        clip: true

        ColumnLayout {
            width: scroll.availableWidth

            ColumnLayout {
                Layout.alignment: Qt.AlignHCenter
                Layout.fillWidth: true
                Layout.maximumWidth: root.cardWidth > 0 ? root.cardWidth : Kirigami.Units.gridUnit * 30
                spacing: Kirigami.Units.largeSpacing

                Kirigami.Heading {
                    Layout.fillWidth: true
                    level: 2
                    wrapMode: Text.WordWrap
                    text: root.tr("Apps", "แอป")
                }

                QQC2.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: root.tr("Tick the apps you want. They download in the background after setup. "
                          + "You can change this any time in Forge Settings, Apps. Unticking an app deletes it; "
                          + "apps that are part of the system are hidden instead.",
                          "เลือกแอปที่ต้องการ แอปจะดาวน์โหลดเบื้องหลังหลังตั้งค่าเสร็จ "
                          + "เปลี่ยนได้ทุกเมื่อใน Forge Settings หัวข้อแอป การเอาเครื่องหมายออกจะลบแอปนั้น "
                          + "ส่วนแอปที่เป็นส่วนหนึ่งของระบบจะถูกซ่อนแทน")
                }

                Repeater {
                    model: root.sections

                    ColumnLayout {
                        id: section
                        required property var modelData
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing

                        Kirigami.Heading {
                            Layout.topMargin: Kirigami.Units.largeSpacing
                            level: 4
                            text: root.tr(section.modelData.en, section.modelData.th)
                        }

                        Repeater {
                            model: root.apps.filter(a => a.section === section.modelData.id)

                            ColumnLayout {
                                id: row
                                required property var modelData
                                Layout.fillWidth: true
                                spacing: 0

                                QQC2.CheckBox {
                                    Layout.fillWidth: true
                                    text: root.tr(row.modelData.en, row.modelData.th)
                                    checked: root.ticked[row.modelData.id] === true
                                    onToggled: root.setTick(row.modelData.id, checked)
                                }
                                QQC2.Label {
                                    visible: row.modelData.noteEn !== ""
                                    Layout.fillWidth: true
                                    Layout.leftMargin: Kirigami.Units.gridUnit * 2
                                    wrapMode: Text.WordWrap
                                    opacity: 0.7
                                    font: Kirigami.Theme.smallFont
                                    text: root.tr(row.modelData.noteEn, row.modelData.noteTh)
                                }
                            }
                        }
                    }
                }

                Kirigami.Separator {
                    Layout.fillWidth: true
                }

                QQC2.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: root.tr("%1 of %2 apps ticked", "เลือก %1 จาก %2 แอป").arg(root.countTicked()).arg(root.apps.length)
                }
            }
        }
    }
}
