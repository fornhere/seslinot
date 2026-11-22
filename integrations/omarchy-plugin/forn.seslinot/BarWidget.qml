import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui

Panel {
  id: root

  moduleName: "forn.seslinot"
  ipcTarget: "forn.seslinot"
  manageIpc: true

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color accent: Commons.Color.accent
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.45)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // $XDG_RUNTIME_DIR/seslinot/state.json varsayılan konumu
  readonly property string runtimeDir: Quickshell.env("XDG_RUNTIME_DIR") || ("/tmp/seslinot-" + (Quickshell.env("USER") || "user"))
  readonly property string defaultStatePath: runtimeDir + "/seslinot/state.json"
  readonly property string stateFilePath: {
    var custom = String(root.setting("statePath", "") || "").trim()
    return custom !== "" ? custom : defaultStatePath
  }

  // DesktopState alanları
  property string voiceState: "idle"
  property int recordingStartedAt: 0
  property var activeSessions: []
  property var pendingConfirm: []
  property int queuedOfflineCount: 0
  property var recentReports: []

  // Canlı süre sayacı ve klavye imleci
  property int nowEpoch: Math.floor(Date.now() / 1000)
  property int selectedIndex: -1
  property bool cursorActive: false

  readonly property bool isRecording: voiceState === "recording"
  readonly property bool isTranscribing: voiceState === "transcribing"
  readonly property bool isPaused: voiceState === "paused"
  readonly property bool hasActivity: isRecording || isTranscribing || isPaused || activeSessions.length > 0 || pendingConfirm.length > 0

  readonly property string barGlyph: {
    if (isRecording) return "󰍬"
    if (isPaused) return "󰏤"
    if (isTranscribing) return "󰔟"
    if (activeSessions.length > 0) return "󰖟"
    if (pendingConfirm.length > 0) return "󰋗"
    return "󰍬"
  }

  readonly property string barLabel: {
    if (isRecording && recordingStartedAt > 0) return formatElapsed(recordingStartedAt)
    if (activeSessions.length > 0) return String(activeSessions.length)
    if (pendingConfirm.length > 0) return String(pendingConfirm.length)
    return ""
  }

  readonly property color barTintColor: {
    if (isRecording) return urgent
    if (isTranscribing || activeSessions.length > 0 || pendingConfirm.length > 0) return accent
    return Qt.darker(barForeground, 1.35)
  }

  readonly property string voiceStatusText: {
    if (isRecording) return "Kayıt yapılıyor (" + formatElapsed(recordingStartedAt) + ")"
    if (isPaused) return "Kayıt duraklatıldı"
    if (isTranscribing) return "Ses metne dönüştürülüyor"
    return "Beklemede"
  }

  readonly property var navItems: {
    var list = []
    var i
    for (i = 0; i < pendingConfirm.length; i++) {
      list.push({ kind: "confirm", item: pendingConfirm[i] })
    }
    for (i = 0; i < activeSessions.length; i++) {
      list.push({ kind: "active", item: activeSessions[i] })
    }
    for (i = 0; i < recentReports.length; i++) {
      list.push({ kind: "report", item: recentReports[i] })
    }
    return list
  }

  function formatElapsed(startedEpoch) {
    var s = Number(startedEpoch || 0)
    if (!isFinite(s) || s <= 0) return "0:00"
    var diff = Math.max(0, nowEpoch - Math.floor(s))
    var mins = Math.floor(diff / 60)
    var secs = diff % 60
    return mins + ":" + (secs < 10 ? "0" + secs : String(secs))
  }

  function sanitizeJobId(raw) {
    var id = String(raw || "").trim()
    if (!id || id.length > 128 || /[\x00-\x20\x7f-\x9f]/.test(id) || id.charAt(0) === "-") return ""
    return id
  }

  function isSafeObsidianUri(uri) {
    if (typeof uri !== "string" || uri.length === 0 || uri.length > 4096) return false
    if (/[\x00-\x20\x7f-\x9f]/.test(uri)) return false
    return uri.indexOf("obsidian://open?") === 0
  }

  function confirmJob(jobId) {
    var clean = sanitizeJobId(jobId)
    if (!clean) return
    Quickshell.execDetached(["seslinot-ctl", "confirm", clean])
  }

  function dismissJob(jobId) {
    var clean = sanitizeJobId(jobId)
    if (!clean) return
    Quickshell.execDetached(["seslinot-ctl", "dismiss", clean])
  }

  function cancelJob(jobId) {
    var clean = sanitizeJobId(jobId)
    if (!clean) return
    Quickshell.execDetached(["seslinot-ctl", "cancel", clean])
  }

  function toggleRecording() {
    Quickshell.execDetached(["seslinot", "record", "toggle"])
  }

  function openObsidianUri(uri) {
    if (!isSafeObsidianUri(uri)) return
    Quickshell.execDetached(["xdg-open", uri])
    root.close()
  }

  function resetState() {
    voiceState = "idle"
    recordingStartedAt = 0
    activeSessions = []
    pendingConfirm = []
    queuedOfflineCount = 0
    recentReports = []
    clampSelection()
  }

  function parseStateText(rawText) {
    var text = String(rawText || "").trim()
    if (!text) {
      resetState()
      return
    }
    try {
      var parsed = JSON.parse(text)
      if (!parsed || typeof parsed !== "object") {
        resetState()
        return
      }
      voiceState = typeof parsed.voiceState === "string" ? parsed.voiceState : "idle"
      recordingStartedAt = Number(parsed.recordingStartedAt || 0) || 0
      activeSessions = Array.isArray(parsed.activeSessions) ? parsed.activeSessions : []
      pendingConfirm = Array.isArray(parsed.pendingConfirm) ? parsed.pendingConfirm : []
      queuedOfflineCount = Math.max(0, Number(parsed.queuedOfflineCount || 0) || 0)
      recentReports = Array.isArray(parsed.recentReports) ? parsed.recentReports : []
      nowEpoch = Math.floor(Date.now() / 1000)
      clampSelection()
    } catch (e) {
      resetState()
    }
  }

  function reloadState() {
    stateFile.reload()
  }

  function clampSelection() {
    if (navItems.length === 0) {
      selectedIndex = -1
      return
    }
    if (selectedIndex >= navItems.length) {
      selectedIndex = navItems.length - 1
    }
  }

  function moveCursor(dy) {
    if (navItems.length === 0) {
      selectedIndex = -1
      return
    }
    if (!cursorActive || selectedIndex < 0) {
      cursorActive = true
      selectedIndex = dy < 0 ? navItems.length - 1 : 0
      return
    }
    selectedIndex = Math.max(0, Math.min(navItems.length - 1, selectedIndex + dy))
  }

  function activateSelected() {
    if (selectedIndex < 0 || selectedIndex >= navItems.length) return
    var entry = navItems[selectedIndex]
    if (!entry || !entry.item) return
    if (entry.kind === "confirm") {
      confirmJob(entry.item.id)
    } else if (entry.kind === "report") {
      openObsidianUri(entry.item.obsidianUri)
    }
    // Aktif araştırma satırında Enter kazara iptal etmez; iptal için Delete veya İptal düğmesi kullanılır.
  }

  function deleteSelected() {
    if (selectedIndex < 0 || selectedIndex >= navItems.length) return
    var entry = navItems[selectedIndex]
    if (!entry || !entry.item) return
    if (entry.kind === "confirm") {
      dismissJob(entry.item.id)
    } else if (entry.kind === "active") {
      cancelJob(entry.item.id)
    }
  }

  onOpenedChanged: {
    if (opened) {
      cursorActive = false
      selectedIndex = -1
      nowEpoch = Math.floor(Date.now() / 1000)
      reloadState()
    }
  }

  FileView {
    id: stateFile
    path: root.stateFilePath
    watchChanges: true
    printErrors: false
    onLoaded: root.parseStateText(text())
    onFileChanged: reload()
    onLoadFailed: root.resetState()
  }

  ShellIpc {
    target: "forn.seslinot.state"
    function refresh(): string {
      root.reloadState()
      return "ok"
    }
  }

  Timer {
    id: elapsedTicker
    interval: 1000
    repeat: true
    running: root.isRecording || (root.opened && root.activeSessions.length > 0)
    onTriggered: root.nowEpoch = Math.floor(Date.now() / 1000)
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.barGlyph
    labelText: root.barLabel
    active: root.hasActivity
    useActiveColor: true
    activeColor: root.barTintColor
    foreground: root.barForeground
    opacity: 1.0

    SequentialAnimation on opacity {
      running: root.isRecording
      loops: Animation.Infinite
      NumberAnimation { to: 0.45; duration: Style.duration(550); easing.type: Easing.InOutQuad }
      NumberAnimation { to: 1.0; duration: Style.duration(550); easing.type: Easing.InOutQuad }
      onRunningChanged: {
        if (!running) button.opacity = 1.0
      }
    }

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.toggleRecording()
      else if (buttonCode === Qt.MiddleButton) root.reloadState()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight, Style.space(520))

    content: PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      implicitHeight: column.implicitHeight

      onMoveRequested: function(dx, dy) {
        if (dy !== 0) root.moveCursor(dy)
      }
      onActivateRequested: root.activateSelected()
      onReturnRequested: root.activateSelected()
      onDeleteRequested: root.deleteSelected()
      onCloseRequested: root.close()
      onTextKey: function(t, modifiers) {
        if (t === "r" || t === "R") root.reloadState()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        Column {
          id: column
          width: parent.width
          spacing: Style.spacing.md

          // --- Üst başlık ---
          RowLayout {
            width: parent.width
            spacing: Style.spacing.md

            Column {
              Layout.fillWidth: true
              spacing: Style.spacing.xxs

              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: "SesliNot"
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
                color: root.foreground
                elide: Text.ElideRight
              }

              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: root.voiceStatusText
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                color: root.isRecording ? root.urgent : root.dim
                elide: Text.ElideRight
              }
            }

            PanelActionButton {
              iconText: root.isRecording ? "󰓛" : "󰍬"
              tooltipText: root.isRecording ? "Kaydı bitir" : "Sesli not başlat"
              foreground: root.isRecording ? root.urgent : root.foreground
              hoverColor: root.isRecording ? root.urgent : root.accent
              onClicked: root.toggleRecording()
            }

            PanelActionButton {
              iconText: "󰑐"
              tooltipText: "Durumu yenile"
              foreground: root.foreground
              hoverColor: root.accent
              onClicked: root.reloadState()
            }
          }

          // --- Çevrimdışı kuyruk uyarısı ---
          BorderSurface {
            visible: root.queuedOfflineCount > 0
            width: parent.width
            implicitHeight: offlineRow.implicitHeight + Style.spacing.md * 2
            radius: Style.cornerRadius
            color: Util.alpha(root.accent, 0.12)
            borderSpec: Border.controlSpec("normal", root.foreground, root.accent)

            RowLayout {
              id: offlineRow
              anchors.fill: parent
              anchors.margins: Style.spacing.md
              spacing: Style.spacing.md

              Text {
                textFormat: Text.PlainText
                text: "󰖪"
                font.family: root.fontFamily
                font.pixelSize: Style.font.icon
                color: root.accent
              }

              Text {
                Layout.fillWidth: true
                textFormat: Text.PlainText
                text: "Çevrimdışı kuyrukta " + root.queuedOfflineCount + " iş internet bekliyor"
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                color: root.foreground
                wrapMode: Text.WordWrap
              }
            }
          }

          // --- Onay Bekleyen Araştırmalar ---
          PanelSeparator {
            visible: root.pendingConfirm.length > 0
            foreground: root.foreground
          }

          PanelSectionHeader {
            visible: root.pendingConfirm.length > 0
            width: parent.width
            foreground: root.foreground
            fontFamily: root.fontFamily
            text: "ONAY BEKLEYENLER (" + root.pendingConfirm.length + ")"
          }

          Repeater {
            model: root.pendingConfirm

            CursorSurface {
              id: confirmRow
              required property var modelData
              required property int index

              readonly property int flatIdx: index
              width: column.width
              implicitHeight: confirmInner.implicitHeight + Style.spacing.md * 2
              foreground: root.foreground
              accent: root.accent
              hasCursor: root.cursorActive && root.selectedIndex === flatIdx

              MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                onEntered: {
                  root.cursorActive = true
                  root.selectedIndex = confirmRow.flatIdx
                }
                onClicked: root.confirmJob(confirmRow.modelData.id)
              }

              RowLayout {
                id: confirmInner
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.margins: Style.spacing.md
                spacing: Style.spacing.md

                Column {
                  Layout.fillWidth: true
                  spacing: Style.spacing.xxs

                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    text: String(confirmRow.modelData.title || "Başlıksız araştırma")
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    font.bold: true
                    color: root.foreground
                    elide: Text.ElideRight
                  }

                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    text: "Güven: %" + Math.round((Number(confirmRow.modelData.confidence || 0)) * 100)
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    color: root.dim
                    elide: Text.ElideRight
                  }
                }

                PanelActionButton {
                  iconText: "󰄬"
                  tooltipText: "Araştırmayı başlat"
                  foreground: root.accent
                  hoverColor: root.accent
                  onClicked: root.confirmJob(confirmRow.modelData.id)
                }

                PanelActionButton {
                  iconText: "󰅖"
                  tooltipText: "Sadece not kalsın"
                  foreground: root.dim
                  hoverColor: root.urgent
                  onClicked: root.dismissJob(confirmRow.modelData.id)
                }
              }
            }
          }

          // --- Aktif Araştırmalar ---
          PanelSeparator {
            visible: root.activeSessions.length > 0
            foreground: root.foreground
          }

          PanelSectionHeader {
            visible: root.activeSessions.length > 0
            width: parent.width
            foreground: root.foreground
            fontFamily: root.fontFamily
            text: "AKTİF ARAŞTIRMALAR (" + root.activeSessions.length + ")"
          }

          Repeater {
            model: root.activeSessions

            CursorSurface {
              id: activeRow
              required property var modelData
              required property int index

              readonly property int flatIdx: root.pendingConfirm.length + index
              width: column.width
              implicitHeight: activeInner.implicitHeight + Style.spacing.md * 2
              foreground: root.foreground
              accent: root.accent
              hasCursor: root.cursorActive && root.selectedIndex === flatIdx

              MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                onEntered: {
                  root.cursorActive = true
                  root.selectedIndex = activeRow.flatIdx
                }
              }

              RowLayout {
                id: activeInner
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.margins: Style.spacing.md
                spacing: Style.spacing.md

                Column {
                  Layout.fillWidth: true
                  spacing: Style.spacing.xxs

                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    text: String(activeRow.modelData.title || "Derin araştırma")
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    font.bold: true
                    color: root.foreground
                    elide: Text.ElideRight
                  }

                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    text: String(activeRow.modelData.phase || "") + " • " + root.formatElapsed(activeRow.modelData.startedAt)
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    color: root.accent
                    elide: Text.ElideRight
                  }
                }

                PanelActionButton {
                  iconText: "󰜺"
                  tooltipText: "Araştırmayı iptal et"
                  foreground: root.dim
                  hoverColor: root.urgent
                  onClicked: root.cancelJob(activeRow.modelData.id)
                }
              }
            }
          }

          // --- Son Raporlar ---
          PanelSeparator {
            visible: root.recentReports.length > 0
            foreground: root.foreground
          }

          PanelSectionHeader {
            visible: root.recentReports.length > 0
            width: parent.width
            foreground: root.foreground
            fontFamily: root.fontFamily
            text: "SON RAPORLAR"
          }

          Repeater {
            model: root.recentReports

            CursorSurface {
              id: reportRow
              required property var modelData
              required property int index

              readonly property int flatIdx: root.pendingConfirm.length + root.activeSessions.length + index
              width: column.width
              implicitHeight: reportInner.implicitHeight + Style.spacing.md * 2
              foreground: root.foreground
              accent: root.accent
              hasCursor: root.cursorActive && root.selectedIndex === flatIdx

              MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                onEntered: {
                  root.cursorActive = true
                  root.selectedIndex = reportRow.flatIdx
                }
                onClicked: root.openObsidianUri(reportRow.modelData.obsidianUri)
              }

              RowLayout {
                id: reportInner
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.margins: Style.spacing.md
                spacing: Style.spacing.md

                Column {
                  Layout.fillWidth: true
                  spacing: Style.spacing.xxs

                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    text: String(reportRow.modelData.title || "Rapor")
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    color: root.foreground
                    elide: Text.ElideRight
                  }

                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    text: Number(reportRow.modelData.sourceCount || 0) + " doğrulanmış kaynak"
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    color: root.dim
                    elide: Text.ElideRight
                  }
                }

                Text {
                  textFormat: Text.PlainText
                  text: "󰏌"
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.iconSmall
                  color: root.dim
                }
              }
            }
          }

          // --- Boş durum ---
          Text {
            visible: root.pendingConfirm.length === 0 && root.activeSessions.length === 0 && root.recentReports.length === 0 && root.queuedOfflineCount === 0
            width: parent.width
            topPadding: Style.spacing.md
            bottomPadding: Style.spacing.md
            textFormat: Text.PlainText
            text: "Aktif araştırma veya bekleyen onay yok.\nSesli not için SUPER+N veya sağ tık kullanın."
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            color: root.dim
            wrapMode: Text.WordWrap
          }
        }
      }
    }
  }
}
