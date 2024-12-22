/*
    SPDX-FileCopyrightText: 2014 Ashish Madeti <ashishmadeti@gmail.com>
    SPDX-FileCopyrightText: 2016 Kai Uwe Broulik <kde@privat.broulik.de>
    SPDX-FileCopyrightText: 2022 ivan (@ratijas) tkachenko <me@ratijas.tk>

    SPDX-License-Identifier: GPL-2.0-or-later
*/

import QtQuick 2.15
import QtQuick.Layouts 1.1

import org.kde.plasma.core as PlasmaCore
import org.kde.kirigami 2.20 as Kirigami
import org.kde.ksvg 1.0 as KSvg

import org.kde.plasma.plasmoid 2.0

PlasmoidItem {
    id: root

    property bool isActive: false

    preferredRepresentation: fullRepresentation
    toolTipSubText: activeController.description

    Plasmoid.icon: isActive ? "nobara-desktop-show-symbolic" : "nobara-desktop-hide-symbolic";

    Plasmoid.title: activeController.title
    Plasmoid.onActivated: activeController.toggle();
    Plasmoid.backgroundHints: PlasmaCore.Types.NoBackground

    Layout.minimumWidth: Kirigami.Units.iconSizes.medium
    Layout.minimumHeight: Kirigami.Units.iconSizes.medium

    Layout.maximumWidth: Layout.minimumWidth
    Layout.maximumHeight: Layout.minimumHeight

    Layout.preferredWidth: Layout.minimumWidth
    Layout.preferredHeight: Layout.minimumHeight

    readonly property bool inPanel: [PlasmaCore.Types.TopEdge, PlasmaCore.Types.RightEdge, PlasmaCore.Types.BottomEdge, PlasmaCore.Types.LeftEdge]
            .includes(Plasmoid.location)

    readonly property bool vertical: Plasmoid.location === PlasmaCore.Types.RightEdge || Plasmoid.location === PlasmaCore.Types.LeftEdge

    /**
    * @c true if the current applet is Minimize All, @c false if the
    * current applet is Show Desktop.
    */
    readonly property bool isMinimizeAll: Plasmoid.pluginName === "org.kde.plasma.minimizeall"

    readonly property Controller primaryController: isMinimizeAll ? minimizeAllController : peekController

    readonly property Controller activeController: {
        if (minimizeAllController.active) {
            return minimizeAllController;
        } else if (peekController.active) {
            return peekController;
        } else {
            return primaryController;
        }
    }

    function toggleActivation() {
        isActive = !isActive;
        updateIcon();
    }

    function updateIcon() {
    	Plasmoid.icon = isActive ? "nobara-desktop-show-symbolic" : "nobara-desktop-hide-symbolic";
    }

    MouseArea {
        id: mouseArea
        anchors.fill: parent

        activeFocusOnTab: true
        hoverEnabled: true

	onClicked: {
	    Plasmoid.activated();
	    toggleActivation();
	}

        Keys.onPressed: {
            switch (event.key) {
            case Qt.Key_Space:
            case Qt.Key_Enter:
            case Qt.Key_Return:
            case Qt.Key_Select:
                Plasmoid.activated();
                break;
            }
        }

        Accessible.name: Plasmoid.title
        Accessible.description: toolTipSubText
        Accessible.role: Accessible.Button
        Accessible.onPressAction: Plasmoid.activated()

        PeekController {
            id: peekController
        }

        MinimizeAllController {
            id: minimizeAllController
        }

        Kirigami.Icon {
            anchors.fill: parent
            active: mouseArea.containsMouse || activeController.active
            source: Plasmoid.icon
        }

        // also activate when dragging an item over the plasmoid so a user can easily drag data to the desktop
        DropArea {
            anchors.fill: parent
            onEntered: activateTimer.start()
            onExited: activateTimer.stop()
        }

        Timer {
            id: activateTimer
            interval: 250 // to match TaskManager
            onTriggered: Plasmoid.activated()
        }

        PlasmaCore.ToolTipArea {
            id: toolTip
            anchors.fill: parent
            mainText: Plasmoid.title
            subText: toolTipSubText
            textFormat: Text.PlainText
        }

    }

    Plasmoid.contextualActions: [
        PlasmaCore.Action {
            text: minimizeAllController.title
            icon.name: minimizeAllController.active ? "window-restore-symbolic" : "window-minimize-symbolic"
            toolTip: minimizeAllController.description
            enabled: !peekController.active
            onTriggered: minimizeAllController.toggle()
        },
        PlasmaCore.Action {
            text: peekController.title
            icon.name: peekController.active ? "window-symbolic" : "desktop-symbolic"
            toolTip: peekController.description
            enabled: !minimizeAllController.active
            onTriggered: peekController.toggle()
        }
    ]
}
