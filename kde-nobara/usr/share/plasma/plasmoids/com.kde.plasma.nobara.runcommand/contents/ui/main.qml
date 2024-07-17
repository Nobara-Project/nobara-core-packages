/*
 *    SPDX-FileCopyrightText: 2012 Gregor Taetzner <gregor@freenet.de>
 *    SPDX-FileCopyrightText: 2020 Ivan Čukić <ivan.cukic at kde.org>
 *
 *    SPDX-License-Identifier: LGPL-2.0-or-later
 */

import QtQuick
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.plasma5support as Plasma5Support

PlasmoidItem {
    id: root
    visible: false  // Hide the entire widget

    Plasma5Support.DataSource {
        id: executable
        engine: "executable"
        connectedSources: []
        onNewData: function(source, data) {
            disconnectSource(source)
        }

        function exec() {
            executable.connectSource("/usr/share/plasma/look-and-feel/org.nobaraproject.desktop/nobara-apply-theme.sh")
        }
    }

    Component.onCompleted: {
        executable.exec()  // Run the hardcoded command on load
    }

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.MiddleButton | Qt.RightButton
        onPressed: {
            if (mouse.button === Qt.RightButton) {
                // Ignore right-click
                mouse.accepted = true
            }
        }
        onClicked: {
            if (mouse.button === Qt.RightButton) {
                // Ignore right-click
                mouse.accepted = true
            }
        }
    }
}
