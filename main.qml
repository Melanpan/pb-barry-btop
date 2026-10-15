pragma ComponentBehavior: Bound
// btop, a Barry Launcher app (barry_apps): btop's full-screen monitor on the
// bottom screen. QML has no terminal, so the app's service (service.py) runs
// btop in a pseudo-terminal and serves its screen as rows of coloured runs;
// this draws them on a Canvas, a cell at a time where it matters (box lines,
// blocks and braille are drawn, not typed, so they join up with no gaps and
// look the same whatever fonts the device has).
//
// Touch: a tap is a mouse click where btop draws it (its boxes, menus and
// buttons all take clicks), a drag up or down is the mouse wheel (the
// process list scrolls). The bar at the bottom has btop's main keys, a field
// that brings up the on-screen keyboard for the rest, and text size.
// A keyboard works too. Quitting btop (q) closes the app.
import QtCore
import QtQuick

Rectangle {
    id: root
    required property var barry  // from Barry Launcher (AppHost.qml)
    color: root.termBg
    focus: true

    readonly property real s: barry.scale
    readonly property color ink: "#eef0f4"
    readonly property color termBg: "#000000"
    readonly property color termFg: "#cccccc"

    Settings {
        id: saved
        location: root.barry.dataDirUrl + "btop-app.ini"
        category: "view"
        property int fontPx: 20  // at the Thor's scale
    }

    // The first monospace font there is with box drawing and braille.
    readonly property string family: {
        const have = Qt.fontFamilies()
        const want = ["DejaVu Sans Mono", "Noto Sans Mono", "Liberation Mono", "Hack", "Cascadia Mono",
                      "Source Code Pro", "Ubuntu Mono", "Consolas", "Monospace"]
        for (const f of want)
            if (have.indexOf(f) >= 0)
                return f
        return "monospace"
    }
    readonly property int fontPx: Math.max(8, Math.round(saved.fontPx * s))
    FontMetrics {
        id: fm
        font.family: root.family
        font.pixelSize: root.fontPx
    }
    readonly property real cellW: fm.averageCharacterWidth
    readonly property real cellH: Math.ceil(fm.height)

    // The grid that fits; the service's grid (what is drawn) follows it.
    readonly property int fitCols: cellW > 0 ? Math.floor(screenArea.width / cellW) : 0
    readonly property int fitRows: cellH > 0 ? Math.floor(screenArea.height / cellH) : 0
    onFitColsChanged: resizeTimer.restart()
    onFitRowsChanged: resizeTimer.restart()

    // What the service last said.
    property int cols: 0
    property int rows: 0
    property int gen: -1
    property int ver: -1
    property bool mouseOn: false
    property string error: barry.serviceUrl ? "" : "This app needs its service (service.py), which did not start."
    property bool started: false
    property var lines: []   // per row: [[col, text, cells, fg, bg, flags], ...]
    property var dirty: ({}) // rows to paint
    property bool allDirty: true
    property int failures: 0

    Timer {
        id: resizeTimer
        interval: 120
        onTriggered: {
            if (root.fitCols < 20 || root.fitRows < 8 || !root.barry.serviceUrl)
                return
            root.barry.request("POST", "resize", { cols: root.fitCols, rows: root.fitRows }, function(status) {
                if (!root.started && status === 200) {
                    root.started = true
                    root.poll()
                }
            })
        }
    }

    function poll() {
        barry.request("GET", "screen?gen=" + gen + "&since=" + ver + "&wait=1000", null, function(status, r) {
            if (status !== 200 || !r) {
                // The service stops when the app closes; until then, keep asking.
                if (++root.failures > 20)
                    root.error = "Lost the connection to btop's service."
                retryTimer.start()
                return
            }
            root.failures = 0
            root.lastFrame = Date.now()
            root.apply(r)
            if (r.exit !== null && r.exit !== undefined)
                root.barry.close()  // btop quit (q)
            else
                root.nextPoll()
        })
    }
    Timer {
        id: retryTimer
        interval: 500
        onTriggered: root.poll()
    }

    // Watching: at most a frame a second, which saves battery (btop itself
    // updates every 1 s too). For a few seconds after a tap or key: every
    // change at once, so menus and scrolling keep up.
    property real lastFrame: 0
    property real lastInput: 0
    readonly property int idleFrameMs: 1000
    readonly property int busyMs: 3000
    function nextPoll() {
        const wait = Date.now() - lastInput < busyMs ? 0 : Math.max(0, idleFrameMs - (Date.now() - lastFrame))
        if (wait === 0) {
            Qt.callLater(poll)
        } else {
            frameTimer.interval = wait
            frameTimer.start()
        }
    }
    function touched() {
        lastInput = Date.now()
        if (frameTimer.running) {  // waiting out the second: ask now
            frameTimer.stop()
            poll()
        }
    }
    Timer {
        id: frameTimer
        onTriggered: root.poll()
    }

    function apply(r) {
        if (r.error)
            error = r.error
        mouseOn = r.mouse
        if (r.full || r.gen !== gen || r.cols !== cols || r.rows !== rows) {
            const l = []
            for (let y = 0; y < r.rows; y++)
                l.push([])
            lines = l
            cols = r.cols
            rows = r.rows
            allDirty = true
        }
        for (const [y, runs] of r.lines) {
            if (y < lines.length) {
                lines[y] = runs
                dirty[y] = true
            }
        }
        gen = r.gen
        ver = r.v
        if (r.lines.length)
            term.requestPaint()
    }

    // -- input --------------------------------------------------------------

    // Keystrokes go out in order, one request at a time.
    property string outbox: ""
    property bool sending: false
    function send(data) {
        if (!data)
            return
        touched()
        outbox += data
        flush()
    }
    function flush() {
        if (sending || !outbox)
            return
        const data = outbox
        outbox = ""
        sending = true
        barry.request("POST", "input", { data: data }, function() {
            root.sending = false
            root.flush()
        })
    }

    function mouse(button, x, y, kind) {
        if (!mouseOn || cellW <= 0)
            return
        touched()
        const col = Math.max(0, Math.min(cols - 1, Math.floor(x / cellW)))
        const row = Math.max(0, Math.min(rows - 1, Math.floor(y / cellH)))
        barry.request("POST", "mouse", { button: button, col: col, row: row, kind: kind }, null)
    }

    // What a terminal sends for a key; "" for none.
    function keySeq(e) {
        const fkeys = ["\x1bOP", "\x1bOQ", "\x1bOR", "\x1bOS", "\x1b[15~", "\x1b[17~", "\x1b[18~",
                       "\x1b[19~", "\x1b[20~", "\x1b[21~", "\x1b[23~", "\x1b[24~"]
        switch (e.key) {
        case Qt.Key_Up: return "\x1b[A"
        case Qt.Key_Down: return "\x1b[B"
        case Qt.Key_Right: return "\x1b[C"
        case Qt.Key_Left: return "\x1b[D"
        case Qt.Key_Home: return "\x1b[H"
        case Qt.Key_End: return "\x1b[F"
        case Qt.Key_PageUp: return "\x1b[5~"
        case Qt.Key_PageDown: return "\x1b[6~"
        case Qt.Key_Insert: return "\x1b[2~"
        case Qt.Key_Delete: return "\x1b[3~"
        case Qt.Key_Return:
        case Qt.Key_Enter: return "\r"
        case Qt.Key_Backspace: return "\x7f"
        case Qt.Key_Tab: return "\t"
        case Qt.Key_Backtab: return "\x1b[Z"
        case Qt.Key_Escape: return "\x1b"
        }
        if (e.key >= Qt.Key_F1 && e.key <= Qt.Key_F12)
            return fkeys[e.key - Qt.Key_F1]
        if ((e.modifiers & Qt.ControlModifier) && e.key >= Qt.Key_A && e.key <= Qt.Key_Z)
            return String.fromCharCode(e.key - Qt.Key_A + 1)
        if (e.text && e.text.charCodeAt(0) >= 32)
            return ((e.modifiers & Qt.AltModifier) ? "\x1b" : "") + e.text
        return ""
    }

    Keys.onPressed: (e) => {
        const seq = keySeq(e)
        if (seq) {
            send(seq)
            e.accepted = true
        }
    }

    // -- the screen -----------------------------------------------------------

    Item {
        id: screenArea
        anchors { left: parent.left; right: parent.right; top: parent.top; bottom: bar.top }

        Canvas {
            id: term
            anchors.centerIn: parent
            width: root.cols * root.cellW
            height: root.rows * root.cellH
            renderStrategy: Canvas.Cooperative
            onWidthChanged: root.allDirty = true
            onHeightChanged: root.allDirty = true
            onAvailableChanged: if (available) { root.allDirty = true; requestPaint() }

            property string fontNow: ""

            // Box drawing: which ways each character's lines go from the
            // middle of its cell; "*" for a rounded corner.
            readonly property var box: ({
                "─": "lr", "━": "lr", "│": "ud", "┃": "ud", "┌": "rd", "┐": "ld", "└": "ru", "┘": "lu",
                "├": "udr", "┤": "udl", "┬": "lrd", "┴": "lru", "┼": "lrud",
                "╭": "rd*", "╮": "ld*", "╰": "ru*", "╯": "lu*"
            })
            // Blocks: [left, top, width, height] as fractions of the cell.
            readonly property var blocks: ({
                "█": [0, 0, 1, 1], "▀": [0, 0, 1, 0.5], "▄": [0, 0.5, 1, 0.5], "▌": [0, 0, 0.5, 1],
                "▐": [0.5, 0, 0.5, 1], "▁": [0, 0.875, 1, 0.125], "▂": [0, 0.75, 1, 0.25],
                "▃": [0, 0.625, 1, 0.375], "▅": [0, 0.375, 1, 0.625], "▆": [0, 0.25, 1, 0.75],
                "▇": [0, 0.125, 1, 0.875], "■": [0.15, 0.3, 0.7, 0.45]
            })

            function colour(c, fallback) {
                return c === "" ? fallback : c === "fg" ? root.termFg : c === "bg" ? root.termBg : c
            }

            function braille(ch) {
                const c = ch.charCodeAt(0)
                return c >= 0x2800 && c <= 0x28ff
            }

            function special(ch) {
                return braille(ch) || box[ch] !== undefined || blocks[ch] !== undefined
            }

            // Braille (btop's graphs): the raised dots as squares, in a 2 x 4
            // grid. Fonts differ too much here (some draw the unraised dots
            // as rings, which turns a graph into a grid).
            readonly property var dotBits: [[0, 0], [0, 1], [0, 2], [1, 0], [1, 1], [1, 2], [0, 3], [1, 3]]
            function drawBraille(ctx, bits, x, top) {
                const w = root.cellW, h = root.cellH
                const d = Math.max(1, Math.round(Math.min(w / 2, h / 4) * 0.72))
                for (let i = 0; i < 8; i++) {
                    if (!(bits & (1 << i)))
                        continue
                    const [dx, dy] = dotBits[i]
                    ctx.fillRect(Math.round(x + (dx + 0.5) * w / 2 - d / 2),
                                 Math.round(top + (dy + 0.5) * h / 4 - d / 2), d, d)
                }
            }

            function drawSpecial(ctx, ch, x, top) {
                const w = root.cellW, h = root.cellH
                if (braille(ch))
                    return drawBraille(ctx, ch.charCodeAt(0) - 0x2800, x, top)
                const b = blocks[ch]
                if (b) {
                    ctx.fillRect(x + b[0] * w, top + b[1] * h, b[2] * w, b[3] * h)
                    return
                }
                const ways = box[ch]
                const lw = Math.max(1, Math.round(w / 8))
                const cx = Math.floor(x + w / 2 - lw / 2), cy = Math.floor(top + h / 2 - lw / 2)
                if (ways.indexOf("*") >= 0) {
                    // A quarter circle from one edge to the other.
                    const r = Math.min(w, h) / 2
                    const mx = cx + lw / 2, my = cy + lw / 2
                    const ex = ways.indexOf("l") >= 0 ? x : x + w
                    const ey = ways.indexOf("u") >= 0 ? top : top + h
                    ctx.beginPath()
                    ctx.moveTo(ex, my)
                    ctx.arcTo(mx, my, mx, ey, r)
                    ctx.lineTo(mx, ey)
                    ctx.lineWidth = lw
                    ctx.strokeStyle = ctx.fillStyle
                    ctx.stroke()
                    return
                }
                if (ways.indexOf("l") >= 0) ctx.fillRect(x, cy, cx - x + lw, lw)
                if (ways.indexOf("r") >= 0) ctx.fillRect(cx, cy, x + w - cx, lw)
                if (ways.indexOf("u") >= 0) ctx.fillRect(cx, top, lw, cy - top + lw)
                if (ways.indexOf("d") >= 0) ctx.fillRect(cx, cy, lw, top + h - cy)
            }

            // Text across exactly `cells` cells, squeezed or stretched if
            // its glyphs (from a fallback font, say) are wider or narrower.
            function drawText(ctx, text, x, baseline, cells) {
                const want = cells * root.cellW
                const got = ctx.measureText(text).width
                if (got <= 0)
                    return
                if (Math.abs(got - want) < 0.5) {
                    ctx.fillText(text, x, baseline)
                } else {
                    ctx.save()
                    ctx.translate(x, 0)
                    ctx.scale(want / got, 1)
                    ctx.fillText(text, 0, baseline)
                    ctx.restore()
                }
            }

            function paintRow(ctx, y) {
                const w = root.cellW, h = root.cellH
                const top = y * h
                const baseline = top + (h - fm.height) / 2 + fm.ascent
                ctx.fillStyle = root.termBg
                ctx.fillRect(0, top, width, h)
                const runs = root.lines[y] || []
                for (const [col, text, cells, fg, bg, flags] of runs) {
                    const x = col * w
                    if (bg !== "") {
                        ctx.fillStyle = colour(bg, root.termBg)
                        ctx.fillRect(x, top, cells * w, h)
                    }
                    if (!text.trim() || (flags & 32))
                        continue
                    const font = ((flags & 4) ? "italic " : "") + ((flags & 1) ? "bold " : "")
                                 + root.fontPx + "px \"" + root.family + "\""
                    if (font !== fontNow) {
                        ctx.font = font
                        fontNow = font
                    }
                    ctx.fillStyle = colour(fg, root.termFg)
                    ctx.globalAlpha = (flags & 2) ? 0.55 : 1
                    if (text.length !== cells) {
                        // Wide or combining characters: the run as a whole.
                        drawText(ctx, text, x, baseline, cells)
                    } else {
                        let start = 0
                        for (let i = 0; i <= text.length; i++) {
                            const ch = i < text.length ? text[i] : ""
                            if (i < text.length && !special(ch))
                                continue
                            if (i > start) {
                                const part = text.substring(start, i)
                                if (part.trim())
                                    drawText(ctx, part, x + start * w, baseline, i - start)
                            }
                            if (ch)
                                drawSpecial(ctx, ch, x + i * w, top)
                            start = i + 1
                        }
                    }
                    if (flags & 8)
                        ctx.fillRect(x, top + h - Math.max(1, h / 14), cells * w, Math.max(1, h / 14))
                    if (flags & 64)
                        ctx.fillRect(x, top + h / 2, cells * w, Math.max(1, h / 14))
                    ctx.globalAlpha = 1
                }
            }

            onPaint: {
                const ctx = getContext("2d")
                ctx.textBaseline = "alphabetic"
                fontNow = ""
                if (root.allDirty) {
                    root.allDirty = false
                    root.dirty = {}
                    ctx.fillStyle = root.termBg
                    ctx.fillRect(0, 0, width, height)
                    for (let y = 0; y < root.rows; y++)
                        paintRow(ctx, y)
                    return
                }
                const rows = Object.keys(root.dirty)
                root.dirty = {}
                for (const y of rows)
                    if (+y < root.rows)
                        paintRow(ctx, +y)
            }
        }

        // Tap: a click. Drag: the wheel, a step per row moved.
        MouseArea {
            anchors.fill: term
            property real startY: 0
            property real lastY: 0
            property real moved: 0
            property bool dragging: false
            onPressed: (m) => {
                root.forceActiveFocus()
                startY = lastY = m.y
                moved = 0
                dragging = false
            }
            onPositionChanged: (m) => {
                if (!dragging && Math.abs(m.y - startY) > root.cellH * 0.8)
                    dragging = true
                if (!dragging)
                    return
                moved += lastY - m.y
                lastY = m.y
                while (moved >= root.cellH) {        // finger up: down the list
                    root.mouse(65, m.x, m.y, "press")
                    moved -= root.cellH
                }
                while (moved <= -root.cellH) {
                    root.mouse(64, m.x, m.y, "press")
                    moved += root.cellH
                }
            }
            onReleased: (m) => {
                if (!dragging)
                    root.mouse(0, m.x, m.y, "click")
            }
        }

        Text {
            anchors.centerIn: parent
            visible: !root.error && root.rows === 0
            text: "Starting btop…"
            color: root.ink
            opacity: 0.7
            font { family: "Noto Sans"; pixelSize: 32 * root.s }
        }
    }

    // -- the bar ----------------------------------------------------------------

    component Key: Rectangle {
        id: key
        property string label
        property string seq
        property real w: 1
        signal clicked()
        width: 74 * root.s * w
        height: 76 * root.s
        radius: 16 * root.s
        color: tap.pressed ? "#4a4f60" : "#1b1d24"
        border.color: "#3a3e4d"
        border.width: 2 * root.s
        Text {
            anchors.centerIn: parent
            text: key.label
            color: root.ink
            font { family: "Noto Sans"; pixelSize: 24 * root.s; weight: Font.DemiBold }
        }
        TapHandler {
            id: tap
            onTapped: {
                if (key.seq)
                    root.send(key.seq)
                key.clicked()
            }
        }
    }

    Rectangle {
        id: bar
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
        height: 96 * root.s
        color: "#101117"

        Flickable {
            anchors.fill: parent
            anchors.margins: 10 * root.s
            contentWidth: keys.width
            boundsBehavior: Flickable.StopAtBounds
            clip: true

            Row {
                id: keys
                spacing: 8 * root.s
                Key { label: "✕"; onClicked: root.barry.close() }
                Key { label: "Menu"; seq: "m"; w: 1.35 }
                Key { label: "Esc"; seq: "\x1b" }
                Key { label: "↑"; seq: "\x1b[A" }
                Key { label: "↓"; seq: "\x1b[B" }
                Key { label: "←"; seq: "\x1b[D" }
                Key { label: "→"; seq: "\x1b[C" }
                Key { label: "⏎"; seq: "\r" }
                Key { label: "Filter"; seq: "f"; w: 1.3 }
                Key { label: "Preset"; seq: "p"; w: 1.4 }

                // A field for the on-screen keyboard: what is typed goes to
                // btop, the field stays empty.
                Rectangle {
                    width: 150 * root.s
                    height: 76 * root.s
                    radius: 16 * root.s
                    color: "#1b1d24"
                    border.color: typed.activeFocus ? "#8fa8ff" : "#3a3e4d"
                    border.width: 2 * root.s
                    TextInput {
                        id: typed
                        anchors.fill: parent
                        anchors.margins: 14 * root.s
                        verticalAlignment: TextInput.AlignVCenter
                        color: root.ink
                        font { family: "Noto Sans"; pixelSize: 26 * root.s }
                        inputMethodHints: Qt.ImhNoPredictiveText | Qt.ImhNoAutoUppercase
                        Keys.onPressed: (e) => {
                            const seq = root.keySeq(e)
                            if (seq) {
                                root.send(seq)
                                e.accepted = true
                            }
                        }
                        onTextChanged: if (text) { root.send(text); text = "" }
                    }
                    Text {
                        anchors.centerIn: parent
                        visible: !typed.activeFocus
                        text: "⌨ Keys"
                        color: root.ink
                        opacity: 0.6
                        font { family: "Noto Sans"; pixelSize: 26 * root.s }
                    }
                }

                Key { label: "A−"; onClicked: saved.fontPx = Math.max(12, saved.fontPx - 1) }
                Key { label: "A+"; onClicked: saved.fontPx = Math.min(32, saved.fontPx + 1) }
            }
        }
    }

    // -- when btop cannot run ------------------------------------------------------

    Rectangle {
        anchors.fill: parent
        visible: root.error !== ""
        color: "#101117"
        Column {
            anchors.centerIn: parent
            width: parent.width * 0.85
            spacing: 24 * root.s
            Text {
                width: parent.width
                text: "btop could not start"
                color: root.ink
                font { family: "Noto Sans"; pixelSize: 44 * root.s; weight: Font.DemiBold }
            }
            Text {
                width: parent.width
                wrapMode: Text.Wrap
                text: root.error
                color: root.ink
                opacity: 0.7
                font { family: "Noto Sans"; pixelSize: 28 * root.s }
            }
            Key { label: "Close"; w: 3; onClicked: root.barry.close() }
        }
    }
}
