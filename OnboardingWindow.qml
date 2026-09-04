pragma ComponentBehavior: Bound

// The first run: what this thing is, and whether this machine can run it.
//
// Drawn rather than written. There is no artwork in this plugin and there
// should not be — so every card shows the interface itself: the crystal in the
// colours it really uses, the agent badges in their vendors' own colours, the
// folder boundary as the boundary, the access row as the row. A person who
// reads these has already seen the parts they will meet.
//
// One card is not a description at all. Everything this build needs — voxtype,
// the model, the packages, the noise suppression, the daemon, the key — fails
// silently when it is absent: a warning in a log, an exit code on a worker's
// stderr, a key press that records and returns nothing. So the fourth card
// asks `bin/omavoice-check` what is actually here and shows the answer, with
// the command to fix each thing that is not, and a button on the ones that are
// safe for it to run. An introduction that says what the program is, on a
// machine where it cannot work, is a brochure.

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Item {
  id: root

  property bool open: false
  property string backend: "codex"
  property string workspace: ""
  property var consented: []
  property var unrestricted: []
  property var voices: []
  property string currentVoice: ""
  property bool canSpeak: false

  signal voicePicked(string name)
  signal voiceTested()

  signal closed()

  readonly property int pages: 6
  property int page: 0

  // Which card the checks are on. Named rather than written as 3 in four
  // places, because inserting a card is otherwise a bug in three of them.
  readonly property int checksPage: 3

  property string pluginDir: ""
  // What the last check said. Rebuilt whole on every run rather than patched:
  // a row that quietly kept a stale `ok` from two runs ago would be the one
  // thing this card must never do.
  property var checks: []
  property bool checking: false
  // The step being installed, "" when nothing is. One at a time on purpose —
  // two pip installs into the same virtualenv is not a thing to discover.
  property string installing: ""
  property string installLog: ""

  // How many checks are failing. Reduced here rather than counted in a binding
  // inside the heading, so the heading stays a sentence.
  readonly property int missing: {
    let n = 0
    for (let i = 0; i < root.checks.length; i++) if (root.checks[i].ok !== true) n += 1
    return n
  }

  // Starting over every time it is opened. This is shown once on its own and
  // afterwards only when asked for, and someone who asks for it wants it from
  // the beginning.
  onOpenChanged: {
    if (open) {
      page = 0
      Qt.callLater(function () { keyCatcher.forceActiveFocus() })
    }
  }

  function next() {
    if (page + 1 < pages) page += 1
    else root.closed()
  }

  function back() {
    if (page > 0) page -= 1
  }

  // --- the checks ------------------------------------------------------------
  //
  // All of the knowing lives in bin/omavoice-check, none of it here. The same
  // rule the rest of this desktop's plugins follow: a helper emits JSON and the
  // QML draws it. It means the list can be read in a terminal, tested without a
  // shell, and changed without touching an interface.

  function runChecks() {
    if (root.pluginDir === "" || root.checking) return
    root.checking = true
    checker.running = false
    checker.running = true
  }

  function install(row) {
    if (root.installing !== "" || !row || row.runnable !== true) return
    root.installing = String(row.id)
    root.installLog = ""
    // The `fix` string is a command line this program wrote — the check script
    // builds it from its own constants — so the step name is taken from the row
    // id rather than parsed back out of it. Nothing here ever runs a string
    // that came from somewhere else.
    installer.command = ["bash", root.pluginDir + "/scripts/setup.sh", root.stepFor(row.id)]
    installer.running = true
  }

  // Which setup.sh step fixes which check. A short table beats parsing the
  // command out of `fix`, and it is the thing to update when a step is added.
  function stepFor(id) {
    switch (String(id)) {
    case "venv": return "venv"
    case "kokoro-model": return "models"
    case "echo-cancel": return "pipewire"
    case "daemon": return "unit"
    default: return "all"
    }
  }

  function copyFix(text) {
    // argv vector, never a shell string. wl-copy is standard on this desktop
    // and the plugin already reaches for external programs this way.
    Util.execArgv(["wl-copy", String(text || "")])
  }

  Process {
    id: checker
    command: [root.pluginDir + "/bin/omavoice-check", "--json"]
    stdout: StdioCollector {
      onStreamFinished: {
        root.checking = false
        try {
          const parsed = JSON.parse(this.text)
          root.checks = Array.isArray(parsed) ? parsed : []
        } catch (e) {
          // A check script that cannot be parsed is a broken check script, not
          // a broken machine. Saying nothing is better than painting every row
          // red and sending somebody to reinstall things that are fine.
          root.checks = []
        }
      }
    }
  }

  Process {
    id: installer
    stdout: StdioCollector { onStreamFinished: root.installLog = this.text }
    stderr: StdioCollector { onStreamFinished: root.installLog += this.text }
    // onRunningChanged rather than onExited: the exit signal carries a
    // QProcess::ExitStatus that qmllint cannot resolve from here, and nothing
    // below looks at the code anyway — the check decides whether it worked.
    onRunningChanged: {
      if (installer.running) return
      root.installing = ""
      root.runChecks()
    }
  }

  // While the card is on screen, and only then. A fix made in a terminal should
  // show up here without anyone restarting anything, and a timer that kept
  // running behind five other cards would be forking a shell every two seconds
  // for no reason.
  Timer {
    running: root.open && root.page === root.checksPage && root.installing === ""
    interval: 2000
    repeat: true
    triggeredOnStart: true
    onTriggered: root.runChecks()
  }

  // -- palette on the scrim ---------------------------------------------------

  // On a card, not on the scrim. Floating the content straight onto a darkened
  // desktop is what the speed test does, and it works there because two large
  // dials carry their own contrast. Five screens of small type do not: over a
  // photograph the wallpaper reads straight through the words, and the whole
  // thing looks like something that failed to finish loading rather than like
  // part of the program. So the tour sits on the same surface every other
  // window here sits on, and keeps the deep scrim outside it.
  readonly property color ink: Color.menu.text
  readonly property color inkDim: Qt.rgba(
    Color.menu.text.r, Color.menu.text.g, Color.menu.text.b, 0.62)
  readonly property color inkFaint: Qt.rgba(
    Color.menu.text.r, Color.menu.text.g, Color.menu.text.b, 0.34)
  readonly property color hairline: Qt.rgba(
    Color.menu.text.r, Color.menu.text.g, Color.menu.text.b, 0.20)
  readonly property color mark: Color.accent

  StateHues { id: hues }

  // -- the pieces the cards are drawn from ------------------------------------

  // A node in a schematic, in the same thin-bordered language the help window
  // uses — but on the scrim rather than on a card.
  component Node: Rectangle {
    id: node
    required property string title
    property string detail: ""
    property color tone: root.mark
    property real span: 1.0
    width: parent ? parent.width * span : 0
    anchors.horizontalCenter: parent ? parent.horizontalCenter : undefined
    height: nodeBody.implicitHeight + Style.spaceReal(14)
    radius: Style.spaceReal(5)
    color: Qt.rgba(1, 1, 1, 0.03)
    border.width: 1
    border.color: Qt.rgba(node.tone.r, node.tone.g, node.tone.b, 0.45)

    Column {
      id: nodeBody
      anchors.centerIn: parent
      width: parent.width - Style.spaceReal(20)
      spacing: Style.spaceReal(2)

      Text {
        width: parent.width
        text: node.title
        textFormat: Text.PlainText
        horizontalAlignment: Text.AlignHCenter
        color: node.tone
        font.family: Style.font.family
        font.pixelSize: Style.font.body
      }

      Text {
        width: parent.width
        visible: node.detail !== ""
        text: node.detail
        textFormat: Text.PlainText
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.Wrap
        color: root.inkDim
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }
    }
  }

  // The connection between two nodes: square dots, the mark the waveform is
  // built from, with the name of what travels along it.
  component Link: Item {
    id: link
    property string label: ""
    property int dots: 3
    width: parent ? parent.width : 0
    height: Style.spaceReal(26)

    Column {
      anchors.centerIn: parent
      spacing: Style.spaceReal(3)

      Repeater {
        model: link.dots
        Rectangle {
          required property int index
          width: Style.spaceReal(3)
          height: Style.spaceReal(3)
          anchors.horizontalCenter: parent.horizontalCenter
          color: root.mark
          opacity: 0.28 + index * 0.18
        }
      }
    }

    Text {
      visible: link.label !== ""
      anchors.verticalCenter: parent.verticalCenter
      anchors.left: parent.horizontalCenter
      anchors.leftMargin: Style.spaceReal(12)
      text: link.label
      textFormat: Text.PlainText
      color: root.inkFaint
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }
  }

  // A key, boxed so it reads as a key and not as a word.
  component KeyCap: Row {
    id: cap
    required property string key
    required property string what
    spacing: Style.spaceReal(10)

    Rectangle {
      width: Style.spaceReal(38)
      height: capLabel.implicitHeight + Style.spaceReal(7)
      radius: Style.spaceReal(4)
      color: "transparent"
      border.width: 1
      border.color: root.hairline

      Text {
        id: capLabel
        anchors.centerIn: parent
        text: cap.key
        textFormat: Text.PlainText
        color: root.mark
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      text: cap.what
      textFormat: Text.PlainText
      color: root.inkDim
      font.family: Style.font.family
      font.pixelSize: Style.font.bodySmall
    }
  }

  // A small pill, for the things that are conditions rather than parts.
  component Chip: Rectangle {
    id: chip
    required property string text
    property color tone: root.inkDim
    width: chipLabel.implicitWidth + Style.spaceReal(18)
    height: chipLabel.implicitHeight + Style.spaceReal(9)
    radius: height / 2
    color: "transparent"
    border.width: 1
    border.color: Qt.rgba(chip.tone.r, chip.tone.g, chip.tone.b, 0.35)

    Text {
      id: chipLabel
      anchors.centerIn: parent
      text: chip.text
      textFormat: Text.PlainText
      color: chip.tone
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }
  }

  // One path, inside the folder or outside it. The crossed ones are the point
  // of the card: outside, a file does not come back empty — it is not there.
  component PathRow: Row {
    id: pathRow
    required property string path
    required property bool inside
    spacing: Style.spaceReal(8)

    Rectangle {
      width: Style.spaceReal(5)
      height: Style.spaceReal(5)
      anchors.verticalCenter: parent.verticalCenter
      color: pathRow.inside ? root.mark : root.inkFaint
      opacity: pathRow.inside ? 1.0 : 0.6
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      text: pathRow.path
      textFormat: Text.PlainText
      color: pathRow.inside ? root.ink : root.inkFaint
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      visible: !pathRow.inside
      text: "no such file"
      textFormat: Text.PlainText
      color: root.inkFaint
      opacity: 0.7
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
      font.italic: true
    }
  }

  // The access row as it really is: three settings, one of them chosen.
  component Grant: Column {
    id: grant
    required property string agent
    // 0 not allowed · 1 held to the folder · 2 wide. Read off what was actually
    // granted rather than passed in: this card used to illustrate a state
    // nobody was in, while the card before it showed the real folder — so the
    // tour disagreed with itself about whether it was describing this machine.
    readonly property int choice:
      root.consented.indexOf(grant.agent) < 0 ? 0
        : (root.unrestricted.indexOf(grant.agent) >= 0 ? 2 : 1)
    spacing: Style.spaceReal(6)
    width: parent ? parent.width : 0

    // No name beside it: the badge is already the name, in the vendor's own
    // colours, and printing "codex" next to a pill that says codex reads as a
    // rendering fault rather than as a label.
    AgentBadge { agent: grant.agent }

    Row {
      spacing: Style.spaceReal(4)

      Repeater {
        model: ["not allowed", "this folder", "everything"]

        Rectangle {
          id: choiceCell
          required property int index
          required property string modelData
          readonly property bool picked: choiceCell.index === grant.choice
          width: cell.implicitWidth + Style.spaceReal(16)
          height: cell.implicitHeight + Style.spaceReal(8)
          radius: Style.spaceReal(4)
          color: choiceCell.picked
            ? Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.16)
            : "transparent"
          border.width: 1
          border.color: choiceCell.picked
            ? Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.55)
            : root.hairline

          Text {
            id: cell
            anchors.centerIn: parent
            text: choiceCell.modelData
            textFormat: Text.PlainText
            color: choiceCell.picked ? root.mark : root.inkFaint
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }

  // A card's title block. Every card has the same one, so the eye lands in the
  // same place five times rather than hunting.
  component Heading: Column {
    id: heading
    required property string eyebrow
    required property string title
    // Stops short of the dismiss mark in the card's corner. Only the heading
    // needs the clearance, so only the heading gives it up — indenting every
    // card by the same amount would push the whole thing off centre.
    width: parent ? parent.width - Style.spaceReal(26) : 0
    spacing: Style.spaceReal(6)

    Text {
      text: heading.eyebrow.toUpperCase()
      textFormat: Text.PlainText
      color: root.inkFaint
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
      font.bold: true
      font.letterSpacing: 2
    }

    Text {
      width: parent.width
      text: heading.title
      textFormat: Text.PlainText
      wrapMode: Text.Wrap
      color: root.ink
      font.family: Style.font.family
      font.pixelSize: Style.font.heading
    }
  }

  // One row of the checklist. A passing check is one dim line and a tick, and
  // takes as little room as it deserves; a failing one opens to say what is
  // missing, why it matters, and the exact command — selectable, copyable, and
  // with a button when it is ours to run.
  component CheckRow: Column {
    id: check
    required property var row
    readonly property bool ok: check.row.ok === true
    readonly property bool busy: root.installing === String(check.row.id)

    width: parent ? parent.width : 0
    spacing: Style.spaceReal(3)

    Row {
      width: parent.width
      spacing: Style.spaceReal(8)

      Text {
        anchors.verticalCenter: parent.verticalCenter
        // A tick or a cross, not a colour alone: this has to survive being
        // read by someone who does not see the difference between them.
        text: check.ok ? "\u2713" : "\u2715"
        textFormat: Text.PlainText
        color: check.ok ? root.mark : Color.urgent
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }

      Text {
        anchors.verticalCenter: parent.verticalCenter
        text: String(check.row.label)
        textFormat: Text.PlainText
        color: root.ink
        opacity: check.ok ? 0.55 : 1
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }

      Text {
        anchors.verticalCenter: parent.verticalCenter
        text: String(check.row.detail || "")
        textFormat: Text.PlainText
        elide: Text.ElideRight
        width: Math.max(0, check.width - x - Style.spaceReal(8))
        color: check.ok ? root.inkFaint : Color.urgent
        opacity: check.ok ? 1 : 0.85
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }
    }

    // Everything below only when it is needed. A passing row that still
    // explained itself would bury the two lines that matter.
    Column {
      visible: !check.ok
      width: parent.width
      leftPadding: Style.spaceReal(20)
      spacing: Style.spaceReal(5)

      Text {
        width: check.width - Style.spaceReal(28)
        visible: String(check.row.note || "") !== ""
        text: String(check.row.note || "")
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        color: root.inkDim
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }

      Row {
        spacing: Style.spaceReal(8)
        visible: String(check.row.fix || "") !== ""

        Rectangle {
          visible: check.row.runnable === true
          width: doLabel.implicitWidth + Style.spaceReal(18)
          height: doLabel.implicitHeight + Style.spaceReal(9)
          radius: Style.spaceReal(4)
          color: check.busy
            ? Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.16)
            : (doHover.hovered && root.installing === ""
                ? Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.22)
                : Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.12))
          border.width: 1
          border.color: Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.55)
          opacity: root.installing === "" || check.busy ? 1 : 0.4

          Text {
            id: doLabel
            anchors.centerIn: parent
            text: check.busy ? "Working…" : "Install"
            textFormat: Text.PlainText
            color: root.mark
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }

          HoverHandler { id: doHover; cursorShape: Qt.PointingHandCursor }
          TapHandler { onTapped: root.install(check.row) }
        }

        Rectangle {
          width: copyLabel.implicitWidth + Style.spaceReal(16)
          height: copyLabel.implicitHeight + Style.spaceReal(9)
          radius: Style.spaceReal(4)
          color: copyHover.hovered ? Qt.rgba(1, 1, 1, 0.08) : "transparent"
          border.width: 1
          border.color: root.hairline

          Text {
            id: copyLabel
            anchors.centerIn: parent
            text: "Copy command"
            textFormat: Text.PlainText
            color: root.inkDim
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }

          HoverHandler { id: copyHover; cursorShape: Qt.PointingHandCursor }
          TapHandler { onTapped: root.copyFix(check.row.fix) }
        }
      }

      // The command itself, shown rather than hidden behind the button. Most
      // of these need a terminal anyway, and a person is entitled to read what
      // they are about to run before they run it.
      Text {
        width: check.width - Style.spaceReal(28)
        text: String(check.row.fix || "")
        textFormat: Text.PlainText
        wrapMode: Text.WrapAnywhere
        color: root.inkFaint
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }
    }
  }

  // The one line under the picture. One line, because the picture is the
  // explanation and this is only what the picture cannot draw.
  component Note: Text {
    width: parent ? parent.width : 0
    textFormat: Text.PlainText
    wrapMode: Text.Wrap
    color: root.inkDim
    font.family: Style.font.family
    font.pixelSize: Style.font.bodySmall
  }

  // Not a window of its own. It was a layer-shell surface with its own scrim,
  // then briefly a second toplevel — which meant asking a question opened a
  // second window for the compositor to tile beside the first. It is a view
  // inside the panel now: same place, same size, one window on the desktop.
  Item {
    id: window
    anchors.fill: parent
    visible: root.open


    Item {
      id: keyCatcher
      anchors.fill: parent
      focus: true
      Keys.priority: Keys.BeforeItem
      Keys.onPressed: function (event) {
        switch (event.key) {
        case Qt.Key_Escape:
          root.closed(); event.accepted = true; break
        case Qt.Key_Right:
        case Qt.Key_Space:
        case Qt.Key_Return:
        case Qt.Key_Enter:
          root.next(); event.accepted = true; break
        case Qt.Key_Left:
          root.back(); event.accepted = true; break
        }
      }

      // The same surface every other window in this plugin stands on. What was
      // here before — content floating straight on the scrim, the way the
      // shell's speed test does it — reads on a dark wallpaper and disappears
      // on a photograph, and five screens of small type cannot carry their own
      // contrast the way two big dials can.
      BorderSurface {
        id: card
        anchors.fill: parent
        // No radius of its own: the compositor rounds and borders the window.
        radius: 0
        color: "transparent"
        padding: Style.spacing.panelPadding

        Item {
          id: stage
          anchors.fill: parent
          anchors.topMargin: card.contentTopInset
          anchors.rightMargin: card.contentRightInset
          anchors.bottomMargin: card.contentBottomInset
          anchors.leftMargin: card.contentLeftInset

        Item {
          id: deck
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.bottom: foot.top
          anchors.bottomMargin: Style.spacing.panelGap
          clip: true

          Row {
            id: strip
            height: parent.height
            x: -root.page * deck.width
            Behavior on x {
              NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
            }

            // ---- 1. the voice ------------------------------------------------
            Item {
              width: deck.width
              height: deck.height

              Column {
                anchors.left: parent.left
                anchors.right: parent.right
                // Centred rather than pinned to the top: the cards do not hold
                // the same amount, and a short one left a third of the card
                // empty below it, which reads as something missing.
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.spacing.panelGap
                // The card used to be a fixed 596x468 inside a full-screen
                // surface, so it always had room. It is a resizable window
                // now and the compositor decides — tiled into a quarter of a
                // laptop screen it gets about half that. Shrink to fit rather
                // than clip: a card with its heading cut off is worse than a
                // small one, and everything here is drawn rather than typed.
                scale: Math.min(1, (parent.height - Style.spaceReal(12))
                                   / Math.max(1, implicitHeight))
                transformOrigin: Item.Center

                Heading {
                  eyebrow: "The voice"
                  title: "It hears and speaks. Nothing leaves this machine."
                }

                // The panel, in miniature: the crystal, the waveform's mark,
                // and a line of what it heard.
                Rectangle {
                  width: parent.width
                  height: Style.space(122)
                  radius: Style.spaceReal(6)
                  color: Qt.rgba(1, 1, 1, 0.03)
                  border.width: 1
                  border.color: root.hairline

                  Column {
                    anchors.centerIn: parent
                    spacing: Style.spaceReal(12)

                    Item {
                      width: Style.spaceReal(26)
                      height: Style.spaceReal(26)
                      anchors.horizontalCenter: parent.horizontalCenter
                      PrimeRadiant {
                        anchors.fill: parent
                        tint: root.mark
                        voiceState: "listening"
                        level: 0.5
                      }
                    }

                    // Fourteen marks, the shape the real waveform is built
                    // from, breathing rather than reacting to a microphone
                    // that is not open.
                    Row {
                      id: figure
                      anchors.horizontalCenter: parent.horizontalCenter
                      spacing: Style.spaceReal(4)
                      property real phase: 0

                      NumberAnimation on phase {
                        running: root.open && root.page === 0
                        loops: Animation.Infinite
                        from: 0; to: Math.PI * 2
                        duration: 2600
                      }

                      Repeater {
                        model: 14
                        Rectangle {
                          required property int index
                          readonly property real wave:
                            0.35 + 0.65 * Math.abs(Math.sin(figure.phase + index * 0.42))
                          width: Style.spaceReal(3)
                          height: Style.spaceReal(4) + Style.spaceReal(22) * wave
                          anchors.verticalCenter: parent.verticalCenter
                          color: root.mark
                          opacity: 0.35 + 0.5 * wave
                        }
                      }
                    }

                    Text {
                      anchors.horizontalCenter: parent.horizontalCenter
                      text: "“what is taking up space in here?”"
                      textFormat: Text.PlainText
                      color: root.inkFaint
                      font.family: Style.font.family
                      font.pixelSize: Style.font.caption
                    }
                  }
                }

                Link { label: "audio"; dots: 3 }

                // Node anchors itself to its parent's horizontal centre, which
                // a Row cannot allow, so these stack rather than sit side by
                // side. It reads better anyway: two things, in the order they
                // happen.
                Node {
                  title: "voxtype · whisper"
                  detail: "hears you, on this CPU"
                  span: 0.82
                }

                Node {
                  title: "Kokoro 82M"
                  detail: "speaks back, on this CPU"
                  span: 0.82
                }

                Row {
                  anchors.horizontalCenter: parent.horizontalCenter
                  spacing: Style.spaceReal(8)
                  Chip { text: "no API key"; tone: root.mark }
                  Chip { text: "no network" }
                  Chip { text: "on the CPU" }
                }
              }
            }

            // ---- 2. the brain ------------------------------------------------
            Item {
              width: deck.width
              height: deck.height

              Column {
                anchors.left: parent.left
                anchors.right: parent.right
                // Centred rather than pinned to the top: the cards do not hold
                // the same amount, and a short one left a third of the card
                // empty below it, which reads as something missing.
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.spacing.panelGap
                // The card used to be a fixed 596x468 inside a full-screen
                // surface, so it always had room. It is a resizable window
                // now and the compositor decides — tiled into a quarter of a
                // laptop screen it gets about half that. Shrink to fit rather
                // than clip: a card with its heading cut off is worse than a
                // small one, and everything here is drawn rather than typed.
                scale: Math.min(1, (parent.height - Style.spaceReal(12))
                                   / Math.max(1, implicitHeight))
                transformOrigin: Item.Center

                Heading {
                  eyebrow: "The brain"
                  title: "Every question of fact goes to your own agent."
                }

                Node {
                  title: "the voice"
                  detail: "forbidden from answering"
                  tone: root.inkDim
                  span: 0.62
                }

                Link { label: "ask_agent" }

                // The two agents as they appear everywhere else in the plugin,
                // in their vendors' own colours.
                Rectangle {
                  width: parent.width
                  height: grants.implicitHeight + Style.spaceReal(28)
                  radius: Style.spaceReal(6)
                  color: Qt.rgba(1, 1, 1, 0.03)
                  border.width: 1
                  border.color: Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.45)

                  Column {
                    id: grants
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.margins: Style.spaceReal(16)
                    spacing: Style.spaceReal(14)
                    Grant { agent: "codex" }
                    Grant { agent: "claude" }
                  }
                }

                Note {
                  text: "Each agent is off until you allow it by name, and held "
                      + "to the folder unless you widen it. It never writes, in "
                      + "either setting. Change both in Settings ▸ Access."
                }

                Row {
                  spacing: Style.spaceReal(8)
                  Chip { text: "nothing is configured twice"; tone: root.mark }
                  Chip { text: "its own connectors and MCP servers" }
                }
              }
            }

            // ---- 3. the folder -----------------------------------------------
            Item {
              width: deck.width
              height: deck.height

              Column {
                anchors.left: parent.left
                anchors.right: parent.right
                // Centred rather than pinned to the top: the cards do not hold
                // the same amount, and a short one left a third of the card
                // empty below it, which reads as something missing.
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.spacing.panelGap
                // The card used to be a fixed 596x468 inside a full-screen
                // surface, so it always had room. It is a resizable window
                // now and the compositor decides — tiled into a quarter of a
                // laptop screen it gets about half that. Shrink to fit rather
                // than clip: a card with its heading cut off is worse than a
                // small one, and everything here is drawn rather than typed.
                scale: Math.min(1, (parent.height - Style.spaceReal(12))
                                   / Math.max(1, implicitHeight))
                transformOrigin: Item.Center

                Heading {
                  eyebrow: "The folder"
                  title: "One folder is the context — and the wall."
                }

                Rectangle {
                  width: parent.width
                  height: Style.space(126)
                  radius: Style.spaceReal(6)
                  color: Qt.rgba(1, 1, 1, 0.03)
                  border.width: 1
                  border.color: Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.5)

                  Column {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.margins: Style.spaceReal(16)
                    spacing: Style.spaceReal(9)

                    Text {
                      text: root.workspace === ""
                        ? "~/Projects/site"
                        : root.workspace
                      textFormat: Text.PlainText
                      elide: Text.ElideMiddle
                      width: parent.width
                      color: root.mark
                      font.family: Style.font.family
                      font.pixelSize: Style.font.bodySmall
                    }

                    PathRow { path: "src/main.rs";     inside: true }
                    PathRow { path: "notes/todo.md";   inside: true }
                    PathRow { path: "README.md";       inside: true }
                  }
                }

                Column {
                  width: parent.width
                  spacing: Style.spaceReal(9)

                  Text {
                    text: "everywhere else"
                    textFormat: Text.PlainText
                    color: root.inkFaint
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                    font.letterSpacing: 1
                  }

                  PathRow { path: "~/.ssh/id_ed25519"; inside: false }
                  PathRow { path: "~/Documents";       inside: false }
                }

                Note {
                  text: "Outside the folder a file does not come back empty — it "
                      + "does not exist. Choose the folder in Settings ▸ Access."
                }
              }
            }

            // ---- 4. what it needs --------------------------------------------
            //
            // The card this whole file exists for. Everything above describes a
            // program; this one says whether it can run here, and hands over
            // the command when it cannot.
            Item {
              width: deck.width
              height: deck.height

              Column {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.topMargin: Style.spaceReal(4)
                spacing: Style.spacing.panelGap
                // See the note on the other cards: this is a window now, and
                // it is given whatever the layout has.
                scale: Math.min(1, (parent.height - Style.spaceReal(12))
                                   / Math.max(1, implicitHeight))
                transformOrigin: Item.Top

                Heading {
                  id: checksHead
                  eyebrow: "What it needs"
                  title: root.checks.length === 0
                    ? "Looking at this machine…"
                    : (root.missing === 0
                        ? "Everything it needs is here."
                        : root.missing + (root.missing === 1
                            ? " thing is missing." : " things are missing."))
                }

                // Takes whatever the window has left and scrolls the rest.
                // The list is nine rows and a failing one opens to four, so it
                // will not fit on a small screen however it is arranged — but
                // a fixed height was worse: it left the bottom half of a tall
                // window empty while hiding rows inside a 250px box.
                Flickable {
                  width: parent.width
                  height: Math.max(
                    Style.space(120),
                    Math.min(rows.implicitHeight,
                             deck.height - checksHead.implicitHeight
                               - checksNote.implicitHeight
                               - Style.spacing.panelGap * 2
                               - Style.spaceReal(14)))
                  contentHeight: rows.implicitHeight
                  clip: true
                  boundsBehavior: Flickable.StopAtBounds

                  Column {
                    id: rows
                    width: parent.width
                    spacing: Style.spaceReal(10)

                    Repeater {
                      model: root.checks
                      CheckRow { required property var modelData; row: modelData }
                    }
                  }
                }

                Note {
                  id: checksNote
                  text: root.installing !== ""
                    ? "Working. This runs in your home directory and asks for no "
                    + "password; it can take a few minutes the first time."
                    : "Nothing here is installed for you without asking, and "
                    + "anything needing sudo is only ever shown to copy. "
                    + "This list is bin/omavoice-check — it runs in a terminal too."
                }
              }
            }

            // ---- 5. the voice ------------------------------------------------
            Item {
              width: deck.width
              height: deck.height

              Column {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.topMargin: Style.spaceReal(4)
                spacing: Style.spacing.panelGap
                // See the note on the other cards: this is a window now, and
                // it is given whatever the layout has.
                scale: Math.min(1, (parent.height - Style.spaceReal(12))
                                   / Math.max(1, implicitHeight))
                transformOrigin: Item.Top

                Heading {
                  eyebrow: "The voice"
                  title: "Pick one, and hear it."
                }

                Flickable {
                  width: parent.width
                  height: Math.max(
                    Style.space(110),
                    Math.min(voiceFlow.implicitHeight, deck.height - Style.space(210)))
                  contentHeight: voiceFlow.implicitHeight
                  clip: true
                  boundsBehavior: Flickable.StopAtBounds

                  Flow {
                    id: voiceFlow
                    width: parent.width
                    spacing: Style.spaceReal(6)

                    Repeater {
                      model: root.voices

                      Rectangle {
                        id: vchip
                        required property var modelData
                        readonly property bool picked:
                          String(vchip.modelData.name) === root.currentVoice

                        width: vname.implicitWidth + Style.spaceReal(16)
                        height: vname.implicitHeight + Style.spaceReal(9)
                        radius: Style.spaceReal(4)
                        color: vchip.picked
                          ? Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.16)
                          : (vhover.hovered ? Qt.rgba(1, 1, 1, 0.06) : "transparent")
                        border.width: 1
                        border.color: vchip.picked
                          ? Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.55)
                          : root.hairline

                        Text {
                          id: vname
                          anchors.centerIn: parent
                          text: String(vchip.modelData.name)
                          textFormat: Text.PlainText
                          color: vchip.picked ? root.mark : root.inkDim
                          font.family: Style.font.family
                          font.pixelSize: Style.font.caption
                        }

                        HoverHandler { id: vhover; cursorShape: Qt.PointingHandCursor }
                        TapHandler { onTapped: root.voicePicked(String(vchip.modelData.name)) }
                      }
                    }
                  }
                }

                // Enabled only once the model and the packages are actually
                // here. A button that says "hear it" and produces silence is
                // worse than one that says why it cannot yet.
                Row {
                  spacing: Style.spaceReal(10)

                  Rectangle {
                    width: hearLabel.implicitWidth + Style.spaceReal(20)
                    height: hearLabel.implicitHeight + Style.spaceReal(10)
                    radius: Style.spaceReal(4)
                    opacity: root.canSpeak ? 1 : 0.35
                    color: hearHover.hovered && root.canSpeak
                      ? Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.22)
                      : Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.12)
                    border.width: 1
                    border.color: Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.55)

                    Text {
                      id: hearLabel
                      anchors.centerIn: parent
                      text: "\uf028  Hear it"
                      textFormat: Text.PlainText
                      color: root.mark
                      font.family: Style.font.family
                      font.pixelSize: Style.font.caption
                    }

                    HoverHandler {
                      id: hearHover
                      cursorShape: root.canSpeak ? Qt.PointingHandCursor : Qt.ArrowCursor
                    }
                    TapHandler { onTapped: if (root.canSpeak) root.voiceTested() }
                  }

                  Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: root.canSpeak
                      ? root.currentVoice
                      : "the voice is not installed yet — see the card before this"
                    textFormat: Text.PlainText
                    color: root.canSpeak ? root.inkDim : root.inkFaint
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                  }
                }

                Note {
                  text: "Twenty-seven English voices, American and British. "
                      + "Kokoro has no German — the assistant answers in English "
                      + "because that is what it can pronounce."
                }
              }
            }

            // ---- 6. push to talk ---------------------------------------------
            Item {
              width: deck.width
              height: deck.height

              Column {
                anchors.left: parent.left
                anchors.right: parent.right
                // Centred rather than pinned to the top: the cards do not hold
                // the same amount, and a short one left a third of the card
                // empty below it, which reads as something missing.
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.spacing.panelGap
                // The card used to be a fixed 596x468 inside a full-screen
                // surface, so it always had room. It is a resizable window
                // now and the compositor decides — tiled into a quarter of a
                // laptop screen it gets about half that. Shrink to fit rather
                // than clip: a card with its heading cut off is worse than a
                // small one, and everything here is drawn rather than typed.
                scale: Math.min(1, (parent.height - Style.spaceReal(12))
                                   / Math.max(1, implicitHeight))
                transformOrigin: Item.Center

                Heading {
                  eyebrow: "Push to talk"
                  title: "Hold F10. That is the whole of it."
                }

                // The one thing a person has to leave here knowing, given the
                // size it deserves. Everything else on this card is a footnote
                // to it.
                Rectangle {
                  width: parent.width
                  height: Style.space(52)
                  radius: Style.spaceReal(6)
                  color: Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.10)
                  border.width: 1
                  border.color: Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.45)

                  Row {
                    anchors.centerIn: parent
                    spacing: Style.spaceReal(12)

                    Text {
                      anchors.verticalCenter: parent.verticalCenter
                      text: "\uf130"
                      textFormat: Text.PlainText
                      color: root.mark
                      font.family: Style.font.family
                      font.pixelSize: Style.font.heading
                    }

                    KeyCap { key: "F10"; what: "hold, from anywhere" }
                  }
                }

                Note {
                  text: "Holding it opens this window if it is not already up, "
                      + "and lets go when you do. The panel has a button that "
                      + "does the same thing, for when your hands are on the "
                      + "mouse."
                }

                Row {
                  width: parent.width
                  spacing: Style.spaceReal(26)

                  Column {
                    spacing: Style.spaceReal(9)
                    KeyCap { key: "Esc"; what: "put it away" }
                    KeyCap { key: "N";   what: "forget and start over" }
                    KeyCap { key: "I";   what: "cut the answer off" }
                    KeyCap { key: "H";   what: "how this works" }
                  }

                  Column {
                    spacing: Style.spaceReal(11)

                    Repeater {
                      model: [
                        { state: "idle",      name: "resting",   what: "nothing running" },
                        { state: "listening", name: "listening", what: "hearing you" },
                        { state: "thinking",  name: "thinking",  what: "asking the agent" },
                        { state: "speaking",  name: "speaking",  what: "its turn" },
                        { state: "error",     name: "broken",    what: "N starts over" }
                      ]

                      Row {
                        id: hueRow
                        required property var modelData
                        spacing: Style.spaceReal(9)
                        // A real colour, not the string "black": colorFor reads
                        // .r/.g/.b off it to decide how light the hue should be,
                        // and a string makes that NaN — which lands on the dark
                        // branch and paints every state too dark to read here.
                        readonly property color hue: hues.colorFor(
                          hueRow.modelData.state, Color.menu.background, root.mark)

                        Rectangle {
                          width: Style.spaceReal(8)
                          height: Style.spaceReal(8)
                          anchors.verticalCenter: parent.verticalCenter
                          color: hueRow.hue
                        }

                        Text {
                          width: Style.spaceReal(66)
                          anchors.verticalCenter: parent.verticalCenter
                          text: hueRow.modelData.name
                          textFormat: Text.PlainText
                          color: hueRow.hue
                          font.family: Style.font.family
                          font.pixelSize: Style.font.bodySmall
                        }

                        Text {
                          anchors.verticalCenter: parent.verticalCenter
                          text: hueRow.modelData.what
                          textFormat: Text.PlainText
                          color: root.inkDim
                          font.family: Style.font.family
                          font.pixelSize: Style.font.caption
                        }
                      }
                    }
                  }
                }

                Note {
                  text: "Plain letters, because the panel has nothing to type "
                      + "into. A halo around the crystal in the bar means the "
                      + "microphone is open — right-click it to shut."
                }
              }
            }
          }
        }

        // -- dots, skip, done ---------------------------------------------------

        Item {
          id: foot
          anchors.bottom: parent.bottom
          anchors.left: parent.left
          anchors.right: parent.right
          height: Style.spaceReal(30)

          Text {
            id: skip
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            visible: root.page + 1 < root.pages
            text: "Skip"
            textFormat: Text.PlainText
            color: skipHover.hovered ? root.ink : root.inkFaint
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall

            HoverHandler { id: skipHover }
            TapHandler { onTapped: root.closed() }
          }

          Row {
            anchors.centerIn: parent
            spacing: Style.spaceReal(8)

            Repeater {
              model: root.pages

              Rectangle {
                id: dot
                required property int index
                readonly property bool here: dot.index === root.page
                // The one you are on is a dash, not a brighter dot: on a
                // five-dot row a difference in colour alone is a difference
                // nobody counts.
                width: Style.spaceReal(dot.here ? 16 : 5)
                height: Style.spaceReal(5)
                radius: Style.spaceReal(2)
                anchors.verticalCenter: parent.verticalCenter
                color: dot.here ? root.mark : root.inkFaint

                Behavior on width {
                  NumberAnimation { duration: 200; easing.type: Easing.OutCubic }
                }

                TapHandler { onTapped: root.page = dot.index }
              }
            }
          }

          Rectangle {
            id: nextButton
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            width: nextLabel.implicitWidth + Style.spaceReal(26)
            height: nextLabel.implicitHeight + Style.spaceReal(11)
            radius: Style.spaceReal(4)
            color: nextHover.hovered
              ? Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.18)
              : Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.08)
            border.width: 1
            border.color: Qt.rgba(root.mark.r, root.mark.g, root.mark.b, 0.55)

            Text {
              id: nextLabel
              anchors.centerIn: parent
              text: root.page + 1 < root.pages ? "Next" : "Start talking"
              textFormat: Text.PlainText
              color: root.mark
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }

            HoverHandler { id: nextHover }
            TapHandler { onTapped: root.next() }
          }
        }

        // Dismiss in the card's own corner. The scrim and Esc do the same
        // thing; this is the one a person looks for first. The headings stop
        // short of it rather than running underneath.
        Text {
          id: closeMark
          anchors.top: parent.top
          anchors.right: parent.right
          text: "✕"
          textFormat: Text.PlainText
          color: closeHover.hovered ? root.ink : root.inkFaint
          font.family: Style.font.family
          font.pixelSize: Style.font.subtitle

          HoverHandler { id: closeHover }
          TapHandler { onTapped: root.closed() }
        }
        }
      }
    }
  }
}
