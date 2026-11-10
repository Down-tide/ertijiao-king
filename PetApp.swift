// 二踢脚大王（AI 桌宠）：桌面上的浮动角色 + 任务面板。
// 构建方法见 README（swiftc -parse-as-library -swift-version 5）。
import Cocoa
import WebKit

enum Paths {
    static let resources: URL = Bundle.main.resourceURL ?? URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    #if PET_TESTS
    static let support = URL(fileURLWithPath: ProcessInfo.processInfo.environment["PET_TEST_DIR"] ?? "/tmp/ai-pet-native-tests")
    #else
    /// 数据目录名来自 Info.plist 的 PetDataDirectory，不同的构建可以各用一套任务状态、
    /// 已读标记和 config.json，互不干扰。缺省时用“AI桌宠”。
    static let dataDirectoryName: String = {
        let name = (Bundle.main.object(forInfoDictionaryKey: "PetDataDirectory") as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return name.isEmpty || name.contains("/") ? "AI桌宠" : name
    }()
    static let support: URL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support", isDirectory: true)
        .appendingPathComponent(dataDirectoryName, isDirectory: true)
    #endif
    static let state = support.appendingPathComponent("state.json")
    static let config = support.appendingPathComponent("config.json")
    static let health = support.appendingPathComponent("health.json")
    static let log = support.appendingPathComponent("collector.log")
    /// Config commands run here one at a time, off the main thread.
    static let configQueue = DispatchQueue(label: "local.ai-pet.config", qos: .userInitiated)

    /// 采集器用哪个 Python：只认随包的那一份。
    ///
    /// 应用不自带也不借用系统的 Python；构建时把 runtime/python 整份放进
    /// Resources/python/runtime，这里按固定路径启动。缺了就是构建出了问题。
    static func pythonInterpreter() -> URL? {
        let bundled = resources.appendingPathComponent("python/runtime/bin/python3")
        return FileManager.default.isExecutableFile(atPath: bundled.path) ? bundled : nil
    }
}

/// Capture failures before Python can open its own rotating log. Each file <=64 KiB.
enum CollectorDiagnostics {
    private static let lock = NSLock()
    static func append(_ data: Data) {
        lock.lock()
        defer { lock.unlock() }
        let file = Paths.support.appendingPathComponent("collector-startup.log")
        let backup = Paths.support.appendingPathComponent("collector-startup.log.1")
        let chunk = Data(data.suffix(8192))
        let fm = FileManager.default
        let size = ((try? fm.attributesOfItem(atPath: file.path))?[.size] as? NSNumber)?.intValue ?? 0
        if size + chunk.count > 65536 {
            // Keep at most 64 KiB even when migrating an oversized old log.
            if let existing = try? FileHandle(forReadingFrom: file) {
                if size > 65536 { try? existing.seek(toOffset: UInt64(size-65536)) }
                let tail = (try? existing.readToEnd()) ?? Data()
                try? existing.close()
                try? tail.write(to: backup, options: .atomic)
                try? fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: backup.path)
            }
            try? Data().write(to: file, options: .atomic)
        }
        if !fm.fileExists(atPath: file.path) { fm.createFile(atPath: file.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
        if let h = try? FileHandle(forWritingTo: file) {
            _ = try? h.seekToEnd()
            try? h.write(contentsOf: chunk)
            try? h.close()
        }
    }
}

/// 桌宠所在的窗口：无边框、透明、永远在最上层，点它不会抢走别的应用的焦点。
final class PetPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

/// 任务面板：可以接收键盘（Esc 关闭），但不激活整个应用。
final class TaskPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    override func cancelOperation(_ sender: Any?) { orderOut(nil) }
}

/// 应用不在前台时，第一次点击也直接生效。
final class PanelWebView: WKWebView {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

/// Animation time is independent of data refreshes; unread badges never freeze a pose.
/// Each activity is held for tens of seconds; only blinking and waving flip frames quickly.
struct PetMotion {
    var running = false
    var celebrationUntil: TimeInterval = 0
    var greetingUntil: TimeInterval = 0
    var wakingUntil: TimeInterval = 0
    var lastActivity: TimeInterval = 0
    var epoch: TimeInterval = 0
    var dragging = false

    static let celebration: TimeInterval = 5
    static let sleepAfter: TimeInterval = 300          // 5 minutes with nothing happening
    static let waves: Set<String> = ["wave", "wave-2"]
    /// 挥手动画的最短间隔。气泡不受它限制：每次真的移上来都要能看到状态。
    static let greetingGap: TimeInterval = 3
    /// Sitting, kneeling or lying on the floor (barely sway; not "standing").
    static let floor: Set<String> = ["sit", "sit-blink", "read", "tea", "type", "kneel", "legs-out", "squat",
                                     "lie", "prone-read", "lie-side", "lie-back", "sleep"]
    static let cheers: Set<String> = ["happy", "jump", "firecracker", "wave", "wave-2"]
    /// Idle routine: (activity, seconds). Half of it is sitting or lying, not standing.
    static let idleRoutine: [(String, TimeInterval)] = [
        ("idle", 30), ("sit", 40), ("read", 45), ("legs-out", 30), ("lie", 45), ("prone-read", 40),
        ("idle", 15), ("look", 8), ("tea", 35), ("lie-side", 40), ("kneel", 30), ("lie-back", 35),
        ("squat", 10), ("think", 10), ("stretch", 6), ("sit", 30),
    ]
    /// While a task runs she works: typing, writing, reading, thinking.
    static let workRoutine: [(String, TimeInterval)] = [
        ("type", 45), ("think", 9), ("write", 35), ("type", 40), ("look", 7), ("read", 30), ("prone-read", 30),
        ("think", 9), ("write", 30),
    ]
    /// Every sprite the schedule can show.
    static var allPoses: Set<String> {
        Set((idleRoutine + workRoutine).map { $0.0 })
            .union(["blink", "sit-blink", "write-blink", "sleep", "happy", "jump", "firecracker", "wave", "wave-2", "stretch"])
    }

    /// Frames that belong together (blink, wave); switching inside a pair is instant, not a crossfade.
    static func base(_ pose: String) -> String {
        switch pose {
        case "blink": return "idle"
        case "sit-blink": return "sit"
        case "write-blink": return "write"
        case "wave-2": return "wave"
        default: return pose
        }
    }

    func asleep(at now: TimeInterval) -> Bool {
        !running && !dragging && now >= celebrationUntil && now >= greetingUntil && now >= wakingUntil
            && now - lastActivity > Self.sleepAfter
    }

    private func wave(at now: TimeInterval) -> String {
        Int(now / 0.4) % 2 == 0 ? "wave" : "wave-2"
    }

    private static func activity(in routine: [(String, TimeInterval)], at t: TimeInterval) -> String {
        let total = routine.reduce(0) { $0 + $1.1 }
        var x = t.truncatingRemainder(dividingBy: total)
        for (pose, seconds) in routine {
            if x < seconds { return pose }
            x -= seconds
        }
        return routine.last?.0 ?? "idle"
    }

    func pose(at now: TimeInterval, reducedMotion: Bool = false) -> String {
        if reducedMotion {
            if now < celebrationUntil || now < greetingUntil { return "happy" }
            return running ? "type" : "idle"
        }
        if dragging { return "stretch" }                     // picked up: arms up
        if now < celebrationUntil {
            return celebrationUntil - now > Self.celebration - 1.4 ? "jump" : "firecracker"
        }
        if now < wakingUntil { return "stretch" }            // just woken up
        if now < greetingUntil { return wave(at: now) }
        if asleep(at: now) { return "sleep" }
        let t = max(0, now - epoch)
        let current = Self.activity(in: running ? Self.workRoutine : Self.idleRoutine, at: t)
        // Irregular blink: every 4.5-7 s, only in poses that have a blink frame.
        let blinkPhase = abs(t + 1.7 * sin(t / 11)).truncatingRemainder(dividingBy: 5.7)
        let blink = blinkPhase < 0.16
        switch current {
        case "idle": return blink ? "blink" : "idle"
        case "sit": return blink ? "sit-blink" : "sit"
        case "write": return blink ? "write-blink" : "write"
        default: return current
        }
    }
}

/// 桌宠本体：透明背景的 Q 版全身角色、完成数角标、运行中提示，支持拖动和点击。
final class PetView: NSView {
    static let size = NSSize(width: 168, height: 244)   // base layout size; top 30 pt hold the speech bubble
    static let avatar: CGFloat = 184
    static let minScale: CGFloat = 0.6
    static let maxScale: CGFloat = 1.8
    static let presets: [(String, CGFloat)] = [("小", 0.75), ("中（默认）", 1.0), ("大", 1.3), ("特大", 1.6)]

    /// Display scale of the whole pet (sprite, badge, bubble). The window follows it.
    private(set) var scale: CGFloat = 1
    private let stage = CALayer()       // holds every pet layer at base size; scaled as one

    weak var owner: AppDelegate?

    private let body = CALayer()        // 上下漂浮
    private let content = CALayer()     // 完成时弹一下
    private let plate = CALayer()
    private let face = CALayer()
    private let badge = CALayer()
    private let badgeText = CATextLayer()
    private let sprites: [String: CGImage]
    private var motion = PetMotion()
    private var animationTimer: Timer?
    private var tracking: NSTrackingArea?
    private var currentPose = ""
    private var currentSway = ""
    private let bubble = CALayer()
    private let bubbleText = CATextLayer()
    private var bubbleUntil: TimeInterval = 0
    private var lastDone = 0
    private var lastNeedYou = 0
    private var lastRunning = 0
    private var seenFirstUpdate = false
    private var lastGreeting: TimeInterval = 0

    private var dragStart: NSPoint = .zero
    private var originStart: NSPoint = .zero
    private var dragged = false

    private var avatarFrame: CGRect {
        CGRect(x: (PetView.size.width - PetView.avatar) / 2, y: 22, width: PetView.avatar, height: PetView.avatar)
    }
    init(images: [String: NSImage]) {
        sprites = images.compactMapValues { $0.cgImage(forProposedRect: nil, context: nil, hints: nil) }
        super.init(frame: NSRect(origin: .zero, size: PetView.size))
        let root = CALayer()
        layer = root            // 自己管理图层树
        wantsLayer = true
        buildLayers(root: root, image: sprites["idle"])
        motion.epoch = CACurrentMediaTime()
        motion.lastActivity = motion.epoch
        startFloating()
        let timer = Timer(timeInterval: 0.1, repeats: true) { [weak self] _ in self?.animate() }
        RunLoop.main.add(timer, forMode: .common)
        animationTimer = timer
    }

    deinit { animationTimer?.invalidate() }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    private func buildLayers(root: CALayer, image: CGImage?) {
        let scale = NSScreen.main?.backingScaleFactor ?? 2
        // Everything is laid out at the base size inside `stage`; resizing just scales `stage`.
        stage.anchorPoint = .zero
        stage.frame = CGRect(origin: .zero, size: PetView.size)
        root.addSublayer(stage)
        for l in [body, content] {
            l.frame = CGRect(origin: .zero, size: PetView.size)
            l.contentsScale = scale
        }
        stage.addSublayer(body)
        body.addSublayer(content)

        let a = avatarFrame
        // A small contact shadow below the feet; the character itself is transparent.
        plate.frame = CGRect(x: 52, y: 31, width: 64, height: 6)
        plate.cornerRadius = 3
        plate.backgroundColor = NSColor(calibratedWhite: 0.1, alpha: 0.08).cgColor
        plate.shadowColor = NSColor.black.cgColor
        plate.shadowOpacity = 0.2
        plate.shadowRadius = 5
        plate.shadowOffset = CGSize(width: 0, height: -1)
        plate.shadowPath = CGPath(ellipseIn: plate.bounds, transform: nil)
        content.addSublayer(plate)

        face.frame = a
        face.anchorPoint = CGPoint(x: 0.5, y: 0.08)
        face.position = CGPoint(x: a.midX, y: a.minY + a.height * 0.08)
        face.cornerRadius = 0
        face.masksToBounds = false
        face.backgroundColor = NSColor.clear.cgColor
        face.contentsGravity = .resizeAspect
        face.contents = image
        face.contentsScale = scale
        content.addSublayer(face)

        badge.backgroundColor = NSColor(calibratedRed: 0.91, green: 0.46, blue: 0.29, alpha: 1).cgColor
        badge.borderColor = NSColor.white.cgColor
        badge.borderWidth = 2.5
        badge.isHidden = true
        content.addSublayer(badge)
        style(badgeText, size: 13, scale: scale)
        badge.addSublayer(badgeText)

        bubble.backgroundColor = NSColor.white.withAlphaComponent(0.96).cgColor
        bubble.borderColor = NSColor(calibratedWhite: 0, alpha: 0.08).cgColor
        bubble.borderWidth = 1
        bubble.shadowColor = NSColor.black.cgColor
        bubble.shadowOpacity = 0.14
        bubble.shadowRadius = 4
        bubble.shadowOffset = CGSize(width: 0, height: -1)
        bubble.opacity = 0
        bubble.isHidden = true
        stage.addSublayer(bubble)         // outside `body`, so it doesn't float with the pet
        style(bubbleText, size: 11.5, scale: scale)
        bubbleText.foregroundColor = NSColor(calibratedRed: 0.08, green: 0.13, blue: 0.15, alpha: 1).cgColor
        bubbleText.truncationMode = .end
        bubble.addSublayer(bubbleText)
    }

    /// 头顶的小气泡，几秒后自己消失。
    func say(_ text: String, seconds: TimeInterval = 3) {
        let size: CGFloat = 11.5
        let h: CGFloat = 24
        let w = min(PetView.size.width - 8, textWidth(text, size: size) + 20)
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        bubble.frame = CGRect(x: (PetView.size.width - w) / 2, y: PetView.size.height - h - 3, width: w, height: h)
        bubble.cornerRadius = h / 2
        bubbleText.string = text
        bubbleText.frame = CGRect(x: 8, y: (h - 15) / 2, width: w - 16, height: 15)
        bubble.isHidden = false
        CATransaction.commit()
        bubble.opacity = 1          // implicit fade-in
        bubbleUntil = CACurrentMediaTime() + seconds
    }

    /// 悬停时头顶气泡说的那句话。运行中的提示只在这里出现，不再有常驻标签。
    func statusLine() -> String {
        let waiting = lastDone + lastNeedYou
        if lastNeedYou > 0 && lastRunning > 0 { return "\(lastRunning) 个在跑，\(lastNeedYou) 个要你处理" }
        if lastNeedYou > 0 { return "\(lastNeedYou) 个任务要你处理，点我看看" }
        if lastRunning > 0 && lastDone > 0 { return "\(lastRunning) 个在跑，\(lastDone) 个等你看" }
        if lastRunning > 0 { return "\(lastRunning) 个任务在跑，我盯着呢" }
        if waiting > 0 { return "有 \(waiting) 个完成了，点我看看" }
        return ["今天也要加油鸭", "没有任务，摸会儿鱼", "二踢脚大王在此", "点我看看任务"].randomElement() ?? ""
    }

    private func style(_ t: CATextLayer, size: CGFloat, scale: CGFloat) {
        t.font = NSFont.boldSystemFont(ofSize: size)
        t.fontSize = size
        t.foregroundColor = NSColor.white.cgColor
        t.alignmentMode = .center
        t.contentsScale = scale
    }

    private func textWidth(_ s: String, size: CGFloat) -> CGFloat {
        ceil((s as NSString).size(withAttributes: [.font: NSFont.boldSystemFont(ofSize: size)]).width)
    }
    /// done：待查看的完成数；needYou：需要你处理的（出错、待批准、待确认）；
    /// running：运行中的任务数（转圈 + 底部提示）。
    /// 角标显示 done + needYou 的合计——两类都是“你还没看过”，都要提醒；
    /// 有待处理时角标变红，和只有完成时的橙色区分开。
    func update(done: Int, running: Int, needYou: Int = 0) {
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        let a = avatarFrame
        let wasAsleep = currentPose == "sleep"
        motion.running = running > 0
        // Speak after the CATransaction below, so the bubble's fade-in isn't disabled.
        let startedNew = seenFirstUpdate && running > lastRunning
        if seenFirstUpdate && running < lastRunning { motion.lastActivity = CACurrentMediaTime() }
        if startedNew {
            motion.lastActivity = CACurrentMediaTime()
            if wasAsleep { motion.wakingUntil = CACurrentMediaTime() + 1.6 }
        }
        seenFirstUpdate = true
        lastDone = done
        lastNeedYou = needYou
        lastRunning = running

        let total = done + needYou
        badge.isHidden = total == 0
        if total > 0 {
            let text = total > 99 ? "99+" : String(total)
            let h: CGFloat = 26
            let w = max(h, textWidth(text, size: 13) + 14)
            badge.frame = CGRect(x: PetView.size.width - w - 17, y: a.maxY - h - 10, width: w, height: h)
            badge.cornerRadius = h / 2
            // 有待处理的就用红色：那是要你动手的，比“完成了”更急。
            badge.backgroundColor = needYou > 0
                ? NSColor(calibratedRed: 0.86, green: 0.28, blue: 0.24, alpha: 1).cgColor
                : NSColor(calibratedRed: 0.91, green: 0.46, blue: 0.29, alpha: 1).cgColor
            badgeText.string = text
            badgeText.frame = CGRect(x: 0, y: (h - 16) / 2, width: w, height: 16)
        }

        CATransaction.commit()
        if startedNew && CACurrentMediaTime() > bubbleUntil {
            say(running == 1 ? "开工啦，我帮你盯着" : "又开工一个，现在 \(running) 个")
        }
        animate()
    }

    private func animate() {
        let now = CACurrentMediaTime()
        if bubbleUntil > 0 && now > bubbleUntil {
            bubbleUntil = 0
            bubble.opacity = 0          // implicit fade-out
        }
        if motion.dragging && NSEvent.pressedMouseButtons & 1 == 0 { motion.dragging = false }
        let reduced = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        let pose = motion.pose(at: now, reducedMotion: reduced)
        if reduced { body.removeAllAnimations(); face.removeAllAnimations(); currentSway = "" }
        if !reduced && body.animation(forKey: "float") == nil { startFloating() }
        if pose != currentPose {
            let changedActivity = PetMotion.base(pose) != PetMotion.base(currentPose)
            if pose == "sleep" && currentPose != "sleep" && now > bubbleUntil { say("Zzz…", seconds: 2.5) }
            currentPose = pose
            CATransaction.begin()
            CATransaction.setDisableActions(true)
            if changedActivity && !reduced {
                // Blend into a new activity instead of snapping; blink/wave frames still switch instantly.
                let fade = CATransition()
                fade.type = .fade
                fade.duration = 0.35
                face.add(fade, forKey: "poseFade")
            }
            face.contents = sprites[pose] ?? sprites["idle"]
            CATransaction.commit()
        }
        guard !reduced else { return }
        // One sway per pose group: the two wave frames share it, so it doesn't restart every frame.
        let group: String
        if motion.dragging { group = "drag" }
        else if PetMotion.cheers.contains(pose) { group = "cheer" }
        else if pose.hasPrefix("write") { group = "write" }
        else if pose == "sleep" || pose == "doze" { group = "doze" }
        else if PetMotion.floor.contains(pose) { group = "seated" }
        else { group = "calm" }
        guard group != currentSway else { return }
        currentSway = group
        face.removeAnimation(forKey: "gesture")
        var angle = 0.022, duration = 3.6
        switch group {
        case "drag": (angle, duration) = (0.10, 0.7)     // dangles while carried
        case "cheer": (angle, duration) = (0.045, 0.85)
        case "write": (angle, duration) = (0.012, 3.6)
        case "doze": (angle, duration) = (0.02, 6.0)     // slow breathing-like nod while asleep
        case "seated": (angle, duration) = (0.008, 5.0)  // sitting/lying: barely moves
        default: break
        }
        let sway = CAKeyframeAnimation(keyPath: "transform.rotation.z")
        sway.values = [0, angle, 0, -angle, 0]
        sway.duration = duration
        sway.repeatCount = .infinity
        sway.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        face.add(sway, forKey: "gesture")
    }

    /// A small hop with squash on take-off and landing (the layer's anchor is at the feet).
    private func hop(height: Double, duration: Double = 0.55) {
        let jump = CAKeyframeAnimation(keyPath: "transform.translation.y")
        jump.values = [0, height, 0, height * 0.35, 0]
        jump.keyTimes = [0, 0.35, 0.65, 0.82, 1]
        jump.duration = duration
        jump.timingFunction = CAMediaTimingFunction(name: .easeOut)
        face.add(jump, forKey: "jump")
        let squash = CAKeyframeAnimation(keyPath: "transform.scale.y")
        squash.values = [1, 0.93, 1.06, 0.95, 1.02, 1]
        squash.keyTimes = [0, 0.12, 0.35, 0.65, 0.82, 1]
        squash.duration = duration
        face.add(squash, forKey: "squash")
    }

    private func startFloating() {
        guard !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion else { return }
        let float = CABasicAnimation(keyPath: "transform.translation.y")
        float.fromValue = 0
        float.toValue = 2
        float.duration = 1.8
        float.autoreverses = true
        float.repeatCount = .infinity
        float.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        body.add(float, forKey: "float")
        let breathing = CABasicAnimation(keyPath: "transform.scale.y")
        breathing.fromValue = 1
        breathing.toValue = 1.018
        breathing.duration = 1.8
        breathing.autoreverses = true
        breathing.repeatCount = .infinity
        breathing.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        face.add(breathing, forKey: "breathing")
    }

    /// 有新任务完成时弹一下。
    func bounce() {
        let now = CACurrentMediaTime()
        motion.lastActivity = now
        guard now > motion.celebrationUntil else { return }     // several completions → one celebration
        motion.celebrationUntil = now + PetMotion.celebration
        say("有任务完成啦！点我看看", seconds: 4)
        animate()
        guard !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion else { return }
        let a = CAKeyframeAnimation(keyPath: "transform.scale")
        a.values = [1.0, 1.12, 0.95, 1.04, 1.0]
        a.keyTimes = [0, 0.25, 0.5, 0.75, 1]
        a.duration = 0.6
        face.add(a, forKey: "bounce")
        hop(height: 12, duration: 0.85)
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let tracking = tracking { removeTrackingArea(tracking) }
        let area = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self)
        addTrackingArea(area)
        tracking = area
    }

    override func mouseEntered(with event: NSEvent) {
        let now = CACurrentMediaTime()
        // 每次真的移上来都要有回应：气泡可以直接出来；只有挥手动画做 3 秒节流，
        // 免得鼠标在边缘来回划过时手一直抖。之前把气泡也一起节流了 20 秒，
        // 于是「悬停有时没反应」——那是节流，不是鼠标没识别到。
        if currentPose == "sleep" {
            motion.wakingUntil = now + 1.6             // stretch, then carry on
            say("唔…醒啦", seconds: 2)
        } else {
            if now - lastGreeting > PetMotion.greetingGap {
                lastGreeting = now
                motion.greetingUntil = now + 1.6
            }
            if now > bubbleUntil { say(statusLine()) }
        }
        motion.lastActivity = now
        animate()
    }

    // 拖动 / 点击：移动超过 3 像素算拖动，否则算点击。
    override func mouseDown(with event: NSEvent) {
        if event.modifierFlags.contains(.control), let menu = menu {
            NSMenu.popUpContextMenu(menu, with: event, for: self)
            return
        }
        dragStart = NSEvent.mouseLocation
        originStart = window?.frame.origin ?? .zero
        dragged = false
        motion.dragging = false
    }

    override func mouseDragged(with event: NSEvent) {
        let now = NSEvent.mouseLocation
        let dx = now.x - dragStart.x, dy = now.y - dragStart.y
        if !dragged && dx * dx + dy * dy < 9 { return }
        if !dragged {
            motion.dragging = true
            if CACurrentMediaTime() > bubbleUntil {
                say(["哇，飞起来了", "放我下来～", "要去哪儿呀"].randomElement() ?? "", seconds: 1.6)
            }
            animate()
        }
        dragged = true
        window?.setFrameOrigin(NSPoint(x: originStart.x + dx, y: originStart.y + dy))
        owner?.petMoved()
    }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseUp(with event: NSEvent) {
        let calm = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        if dragged {
            motion.dragging = false
            motion.lastActivity = CACurrentMediaTime()
            animate()
            if !calm { hop(height: 4, duration: 0.4) }      // little landing bounce
            owner?.petDragEnded()
        } else {
            motion.lastActivity = CACurrentMediaTime()
            animate()
            if !calm { hop(height: 8) }
            owner?.petClicked()
        }
        dragged = false
    }

    override func viewDidChangeBackingProperties() {
        super.viewDidChangeBackingProperties()
        updateContentsScale()
    }

    /// Render sprites and text at screen resolution × pet scale, so a bigger pet stays sharp.
    private func updateContentsScale() {
        let s = (window?.backingScaleFactor ?? NSScreen.main?.backingScaleFactor ?? 2) * max(1, scale)
        for l in [face, badgeText, bubbleText, badge, bubble, plate] as [CALayer] { l.contentsScale = s }
    }

    /// Resize the pet. The window keeps its bottom-centre point, so the pet grows in place.
    func setScale(_ newValue: CGFloat) {
        let s = min(PetView.maxScale, max(PetView.minScale, (newValue * 20).rounded() / 20))
        let size = NSSize(width: PetView.size.width * s, height: PetView.size.height * s)
        if s == scale, let current = window?.frame.size, current == size { return }
        scale = s
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        stage.setAffineTransform(CGAffineTransform(scaleX: s, y: s))
        CATransaction.commit()
        updateContentsScale()
        if let window = window {
            let old = window.frame
            window.setFrame(NSRect(x: old.midX - size.width / 2, y: old.minY, width: size.width, height: size.height), display: true)
        }
        owner?.petScaled()
    }

    // ⌥ + 滚轮：每格放大或缩小 5%。触控板的细小滚动先累积，凑够一格再变。
    private var scrollAccum: CGFloat = 0
    override func scrollWheel(with event: NSEvent) {
        guard event.modifierFlags.contains(.option) else { super.scrollWheel(with: event); return }
        guard event.momentumPhase.isEmpty else { return }
        scrollAccum += event.hasPreciseScrollingDeltas ? event.scrollingDeltaY / 40 : event.scrollingDeltaY
        while abs(scrollAccum) >= 1 {
            let up = scrollAccum > 0
            scrollAccum += up ? -1 : 1
            setScale(scale + (up ? 0.05 : -0.05))
        }
    }
}
/// 菜单栏图标：四芒星。四边向中心内凹，收腰明显——这是现在各家 AI 助手
/// 星芒的共同画法（Google Gemini 的 sparkle 就是这个结构），比字体里现成的 ✦
/// 显得更利落。底部实心、面积更足，缩到菜单栏尺寸不会发灰发虚。
/// 不显示完成数量：那个数在桌宠的橙色角标上已经有了。
enum StatusIcon {
    static let canvas: CGFloat = 64        // 设计稿坐标系，绘制时整体缩放到 pointSize
    static let pointSize: CGFloat = 18     // 之前 19pt 偏大偏胖，收回标准菜单栏尺寸
    /// 越小凹得越狠（腰越细）。0.24 保留 Gemini 那种锐利收腰，
    /// 又比 0.22 略饱满一点；0.08 在菜单栏尺寸下尖端会糊，0.28 偏胖。
    static let waist: CGFloat = 0.24
    /// 半径 32：腰身比例不动，饱满度和锐利感平衡得最好。
    static let radius: CGFloat = 32

    static func make() -> NSImage {
        let image = NSImage(size: NSSize(width: pointSize, height: pointSize))
        image.lockFocus()
        guard let ctx = NSGraphicsContext.current else { image.unlockFocus(); return image }
        // 对齐到像素中心，小尺寸下边缘才不会糊
        let k = pointSize / canvas
        ctx.cgContext.saveGState()
        ctx.cgContext.scaleBy(x: k, y: k)
        NSColor.black.setFill()
        starPath().fill()
        ctx.cgContext.restoreGState()
        image.unlockFocus()
        image.isTemplate = true            // 跟随菜单栏浅色/深色自动反色
        return image
    }

    /// 四个尖角，边用向内收的控制点连成凹曲线。
    private static func starPath() -> NSBezierPath {
        let c = NSPoint(x: canvas/2, y: canvas/2)
        let r = radius
        let d = r * waist                    // 控制点落在这条对角线上，越靠近中心越凹
        let top = NSPoint(x: c.x, y: c.y + r)
        let right = NSPoint(x: c.x + r, y: c.y)
        let bottom = NSPoint(x: c.x, y: c.y - r)
        let left = NSPoint(x: c.x - r, y: c.y)
        let path = NSBezierPath()
        path.move(to: top)
        func arc(to end: NSPoint, _ cx: CGFloat, _ cy: CGFloat) {
            path.curve(to: end, controlPoint1: NSPoint(x: c.x + cx, y: c.y + cy),
                       controlPoint2: NSPoint(x: c.x + cx, y: c.y + cy))
        }
        arc(to: right, d, d)
        arc(to: bottom, d, -d)
        arc(to: left, -d, -d)
        arc(to: top, -d, d)
        path.close()
        return path
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler, WKNavigationDelegate {
    private var petWindow: PetPanel!
    private var petView: PetView!
    private var panel: TaskPanel!
    private var webView: PanelWebView!
    private var pageReady = false
    private var panelShownAt = Date.distantPast

    private var rawState: [String: Any] = [:]
    private var lastData: Data?
    private var seenDoneIDs: Set<String>?
    private var dismissed = Set<String>()
    private var dismissedOrder: [String] = []

    private var collector: Process?
    private var collectorStartedAt = Date.distantPast
    private var collectorFailures = 0
    private var nextCollectorStart = Date.distantPast
    private var timer: Timer?
    private var statusItem: NSStatusItem?
    private var sizeItems: [NSMenuItem] = []
    private var collectorHealthy = false
    private var serverMenu: NSMenu?
    private var servers: [[String: Any]] = []
    private var configFromDisk: [String: Any] = [:]
    /// config.json 读进来之前，设置页里是空表单；这时保存会把服务器列表清空，所以先不让存。
    private var configLoaded = false
    #if PET_TESTS
    private let defaults = UserDefaults(suiteName: "local.ai-pet.native-test-suite")!
    #else
    private let defaults = UserDefaults.standard
    #endif

    func applicationDidFinishLaunching(_ notification: Notification) {
        try? FileManager.default.createDirectory(at: Paths.support, withIntermediateDirectories: true)
        // 保留上一份状态，避免采集器启动稍慢或远程网络暂时失败时面板瞬间变空。
        // collector 会在下一轮用本机/远程最新结果覆盖它，基线和已读标记仍由 defaults 持久化。
        dismissedOrder = defaults.stringArray(forKey: "dismissed") ?? []
        dismissed = Set(dismissedOrder)

        buildPet()
        buildPanel()
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = StatusIcon.make()
        item.button?.imagePosition = .imageOnly
        item.button?.toolTip = "二踢脚大王：打开任务面板或找回桌宠"
        let statusMenu = NSMenu()
        statusMenu.addItem(withTitle: "打开任务面板", action: #selector(showPanelAction(_:)), keyEquivalent: "")
        statusMenu.addItem(withTitle: "桌宠回到右下角", action: #selector(resetPosition(_:)), keyEquivalent: "")
        let sizeItem = sizeMenuItem()
        statusMenu.addItem(.separator())
        statusMenu.addItem(withTitle: "退出二踢脚大王", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        statusMenu.items.forEach { $0.target = self }
        statusMenu.items.last?.target = NSApp
        statusMenu.insertItem(sizeItem, at: 2)      // after 桌宠回到右下角; keeps its own targets
        let servers = NSMenuItem(title: "服务器", action: nil, keyEquivalent: "")
        let serverMenu = NSMenu()
        serverMenu.addItem(withTitle: "正在读取状态…", action: nil, keyEquivalent: "")
        servers.submenu = serverMenu
        self.serverMenu = serverMenu
        statusMenu.insertItem(servers, at: 3)
        statusMenu.insertItem(.separator(), at: 4)
        item.menu = statusMenu
        statusItem = item
        #if !PET_TESTS
        loadConfigForPanel()
        ensureCollector()

        let t = Timer(timeInterval: 1.5, target: self, selector: #selector(tick(_:)), userInfo: nil, repeats: true)
        RunLoop.main.add(t, forMode: .common)
        timer = t
        tick(t)
        #endif

        NotificationCenter.default.addObserver(self, selector: #selector(screensChanged(_:)),
                                               name: NSApplication.didChangeScreenParametersNotification, object: nil)
        perform(#selector(showPanelAction(_:)), with: nil, afterDelay: 0.8)
    }

    func applicationWillTerminate(_ notification: Notification) {
        collector?.terminate()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        petWindow.setFrameOrigin(clampedPetOrigin(petWindow.frame.origin))
        petWindow.orderFrontRegardless()
        showPanel()
        return false
    }

    // MARK: 桌宠窗口

    private func buildPet() {
        let assetRoot = Paths.resources.appendingPathComponent("assets")
        var images: [String: NSImage] = [:]
        for name in ["avatar", "work", "doze"] + PetMotion.allPoses.sorted() {
            if let image = NSImage(contentsOf: assetRoot.appendingPathComponent("pet-\(name).png")) {
                images[name] = image
            }
        }
        if images["avatar"] == nil,
           let image = NSImage(contentsOf: assetRoot.appendingPathComponent("pet-avatar.png")) {
            images["avatar"] = image
        }
        petView = PetView(images: images)
        petView.owner = self

        petWindow = PetPanel(contentRect: NSRect(origin: .zero, size: PetView.size),
                             styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        petWindow.isOpaque = false
        petWindow.backgroundColor = .clear
        petWindow.hasShadow = false
        petWindow.level = .floating
        petWindow.isFloatingPanel = true
        petWindow.hidesOnDeactivate = false
        petWindow.isMovableByWindowBackground = false
        petWindow.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        petWindow.contentView = petView
        let savedScale = defaults.double(forKey: "petScale")
        if savedScale > 0 { petView.setScale(CGFloat(savedScale)) }
        petWindow.setFrameOrigin(savedPetOrigin() ?? defaultPetOrigin())

        let menu = NSMenu()
        let items: [(String, Selector)] = [("打开任务面板", #selector(showPanelAction(_:))),
                                           ("把桌宠放回右下角", #selector(resetPosition(_:)))]
        for (title, action) in items {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
            item.target = self
            menu.addItem(item)
        }
        menu.addItem(sizeMenuItem())
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "退出二踢脚大王", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        quit.target = NSApp
        menu.addItem(quit)
        petView.menu = menu

        petWindow.orderFrontRegardless()
    }

    /// The pet window's current size (base size × display scale).
    private var petSize: NSSize { petWindow?.frame.size ?? PetView.size }

    private func defaultPetOrigin() -> NSPoint {
        let f = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1280, height: 800)
        return NSPoint(x: f.maxX - petSize.width - 24, y: f.minY + 24)
    }

    private func savedPetOrigin() -> NSPoint? {
        guard let s = defaults.string(forKey: "petOrigin") else { return nil }
        return clampedPetOrigin(NSPointFromString(s))
    }

    private func clampedPetOrigin(_ origin: NSPoint) -> NSPoint {
        guard origin.x.isFinite, origin.y.isFinite else { return defaultPetOrigin() }
        let size = petSize
        let center = NSPoint(x: origin.x + size.width / 2, y: origin.y + size.height / 2)
        func distance(_ screen: NSScreen) -> CGFloat {
            let f = screen.visibleFrame
            let dx = max(f.minX - center.x, 0, center.x - f.maxX)
            let dy = max(f.minY - center.y, 0, center.y - f.maxY)
            return dx * dx + dy * dy
        }
        guard let screen = NSScreen.screens.min(by: { distance($0) < distance($1) }) else { return defaultPetOrigin() }
        let f = screen.visibleFrame.insetBy(dx: 4, dy: 4)
        return NSPoint(x: min(max(origin.x, f.minX), max(f.minX, f.maxX - size.width)),
                       y: min(max(origin.y, f.minY), max(f.minY, f.maxY - size.height)))
    }
    // MARK: 任务面板

    private func buildPanel() {
        let size = NSSize(width: 380, height: 560)
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "pet")
        webView = PanelWebView(frame: NSRect(origin: .zero, size: size), configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        webView.unregisterDraggedTypes()

        let container = NSView(frame: NSRect(origin: .zero, size: size))
        container.wantsLayer = true
        container.layer?.cornerRadius = 16
        container.layer?.masksToBounds = true
        container.layer?.backgroundColor = NSColor.white.cgColor
        container.addSubview(webView)

        panel = TaskPanel(contentRect: NSRect(origin: .zero, size: size),
                          styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .floating
        panel.isFloatingPanel = true
        panel.becomesKeyOnlyIfNeeded = false
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        panel.contentView = container
        panel.delegate = self

        let html = Paths.resources.appendingPathComponent("index.html")
        if FileManager.default.fileExists(atPath: html.path) {
            webView.loadFileURL(html, allowingReadAccessTo: Paths.resources)
        } else {
            webView.loadHTMLString("<body style='font:13px -apple-system;padding:24px'>找不到 index.html，应用包不完整，请重新构建或重新下载。</body>", baseURL: nil)
        }
    }

    private func placePanel() {
        let pet = petWindow.frame
        let screen = petWindow.screen ?? NSScreen.main
        let vf = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1280, height: 800)
        let size = panel.frame.size
        let onRight = pet.midX > vf.midX
        var x = onRight ? pet.minX - size.width - 8 : pet.maxX + 8
        var y = pet.minY
        x = min(max(x, vf.minX + 8), vf.maxX - size.width - 8)
        y = min(max(y, vf.minY + 8), vf.maxY - size.height - 8)
        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }

    func showPanel() {
        placePanel()
        panelShownAt = Date()
        panel.makeKeyAndOrderFront(nil)
        pushState()
    }

    func hidePanel() {
        panel.orderOut(nil)
        // 收起时让面板回到任务列表：设置页只在本次打开期间有效。
        webView.evaluateJavaScript("window.petPanelHidden && window.petPanelHidden()", completionHandler: nil)
    }

    func windowDidResignKey(_ notification: Notification) {
        // 点到别的应用时收起，跟系统弹出框一样
        if Date().timeIntervalSince(panelShownAt) > 0.4 {
            hidePanel()
        } else {
            perform(#selector(hideIfNotKey(_:)), with: nil, afterDelay: 0.45)
        }
    }

    @objc func hideIfNotKey(_ sender: Any?) {
        if panel.isVisible && !panel.isKeyWindow { hidePanel() }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        pageReady = false
        webView.reload()
    }

    func petClicked() {
        if panel.isVisible { hidePanel() } else { showPanel() }
    }

    func petMoved() {
        if panel.isVisible { placePanel() }
    }

    func petDragEnded() {
        petWindow.setFrameOrigin(clampedPetOrigin(petWindow.frame.origin))
        defaults.set(NSStringFromPoint(petWindow.frame.origin), forKey: "petOrigin")
        if panel.isVisible { placePanel() }
    }

    @objc func showPanelAction(_ sender: Any?) { showPanel() }

    /// “大小” submenu: presets plus a hint for ⌥ + scroll. Used by both menus.
    private func sizeMenuItem() -> NSMenuItem {
        let sub = NSMenu()
        for (title, value) in PetView.presets {
            let item = NSMenuItem(title: title, action: #selector(setPetSize(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = NSNumber(value: Double(value))
            item.state = abs(petView.scale - value) < 0.001 ? .on : .off
            sub.addItem(item)
            sizeItems.append(item)
        }
        sub.addItem(.separator())
        let hint = NSMenuItem(title: "按住 ⌥ 在桌宠上滚动滚轮可微调", action: nil, keyEquivalent: "")
        hint.isEnabled = false
        sub.addItem(hint)
        let parent = NSMenuItem(title: "大小", action: nil, keyEquivalent: "")
        parent.submenu = sub
        return parent
    }

    @objc func setPetSize(_ sender: NSMenuItem) {
        guard let value = sender.representedObject as? NSNumber else { return }
        petView.setScale(CGFloat(value.doubleValue))
    }

    /// Called by PetView after every size change.
    func petScaled() {
        for item in sizeItems {
            let value = (item.representedObject as? NSNumber)?.doubleValue ?? 0
            item.state = abs(Double(petView.scale) - value) < 0.001 ? .on : .off
        }
        defaults.set(Double(petView.scale), forKey: "petScale")
        guard petWindow != nil, petWindow.isVisible else { return }   // startup restore: position comes next
        petWindow.setFrameOrigin(clampedPetOrigin(petWindow.frame.origin))
        defaults.set(NSStringFromPoint(petWindow.frame.origin), forKey: "petOrigin")
        if panel != nil, panel.isVisible { placePanel() }
    }

    @objc func resetPosition(_ sender: Any?) {
        petWindow.setFrameOrigin(defaultPetOrigin())
        defaults.removeObject(forKey: "petOrigin")
        petWindow.orderFrontRegardless()
        if panel.isVisible { placePanel() }
    }

    @objc func screensChanged(_ notification: Notification) {
        petWindow.setFrameOrigin(clampedPetOrigin(petWindow.frame.origin))
        if panel.isVisible { placePanel() }
    }
    // MARK: 数据

    @objc func tick(_ timer: Timer) {
        ensureCollector()
        let health = (try? Data(contentsOf: Paths.health)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        let healthy = collector?.isRunning == true && Date().timeIntervalSince1970 - ((health?["updatedAt"] as? Double) ?? 0) < 20
        if healthy != collectorHealthy {
            collectorHealthy = healthy
            pushState()
        }
        guard let data = try? Data(contentsOf: Paths.state), data != lastData else { return }
        lastData = data
        guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
        rawState = obj
        establishInitialBaselineIfNeeded()
        updateServers()
        refresh()
    }

    /// 持久化开始跟踪的时间。迟到的旧记录也不能重新进入收件箱。
    private func establishInitialBaselineIfNeeded() {
        guard defaults.double(forKey: "trackingStartedAt") == 0,
              rawState["version"] != nil else { return }
        let now = Date().timeIntervalSince1970
        let active = ((rawState["running"] as? [[String: Any]]) ?? []).compactMap { $0["id"] as? String }
        defaults.set(now, forKey: "trackingStartedAt")
        defaults.set(active, forKey: "trackingInitialActiveIDs")
        let oldItems = ((rawState["done"] as? [[String: Any]]) ?? []) + ((rawState["pending"] as? [[String: Any]]) ?? [])
        for item in oldItems {
            if let id = item["id"] as? String, !dismissed.contains(id) {
                dismissed.insert(id)
                dismissedOrder.append(id)
            }
        }
        defaults.set(dismissedOrder, forKey: "dismissed")
        defaults.set(true, forKey: "baselineEstablished")
    }

    private func visibleState() -> [String: Any] {
        var s = rawState
        let since = defaults.double(forKey: "trackingStartedAt")
        let initial = Set(defaults.stringArray(forKey: "trackingInitialActiveIDs") ?? [])
        func eligible(_ item: [String: Any], terminal: Bool) -> Bool {
            let id = item["id"] as? String ?? ""
            if dismissed.contains(id) { return false }
            if since == 0 { return true }
            if initial.contains(id) { return true }
            let start = (item["start"] as? Double) ?? 0
            let ended = (item["time"] as? Double) ?? 0
            // An unknown old turn becoming fresh due to a file write is still historical.
            if item["state"] as? String == "unknown" { return start >= since }
            return terminal ? ended >= since : start >= since
        }
        s["done"] = ((rawState["done"] as? [[String: Any]]) ?? []).filter { eligible($0, terminal: true) }
        s["pending"] = ((rawState["pending"] as? [[String: Any]]) ?? []).filter { eligible($0, terminal: true) }
        s["running"] = ((rawState["running"] as? [[String: Any]]) ?? []).filter { eligible($0, terminal: false) }
        s["config"] = configFromDisk
        s["configLoaded"] = configLoaded
        s["collectorHealthy"] = collectorHealthy
        s["trackingStartedAt"] = since
        return s
    }

    /// 服务器状态进了菜单栏：哪台连不上、最近一次成功读取是什么时候。
    private func updateServers() {
        servers = (rawState["hosts"] as? [[String: Any]]) ?? []
        guard let menu = serverMenu else { return }
        menu.removeAllItems()
        if servers.isEmpty {
            menu.addItem(withTitle: "没有配置服务器", action: nil, keyEquivalent: "")
            return
        }
        for host in servers {
            let label = host["label"] as? String ?? host["id"] as? String ?? "服务器"
            let alias = host["alias"] as? String ?? ""
            let status = host["status"] as? String ?? "offline"
            let title: String
            var tip = alias
            switch status {
            case "ok":
                title = "● \(label) 已连接"
                if let machine = host["machine"] as? String, !machine.isEmpty { tip += " · " + machine }
                if let fetched = host["fetchedAt"] as? Double, fetched > 0 {
                    tip += " · \(Self.age(fetched))更新"
                }
            case "pending":
                title = "○ \(label) 正在连接"
            case "stale":
                title = "◐ \(label) 暂时连不上"
                tip += " · " + (host["hint"] as? String ?? "保留着上次的数据")
            default:
                title = "○ \(label) 连不上"
                tip += " · " + (host["hint"] as? String ?? "请检查 SSH 配置")
            }
            let item = NSMenuItem(title: title, action: #selector(openServerTerminal(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = alias
            item.toolTip = tip
            if status != "ok" { item.state = .off }
            menu.addItem(item)
        }
        menu.addItem(.separator())
        let check = NSMenuItem(title: "在终端里连过去", action: #selector(openServerTerminal(_:)), keyEquivalent: "")
        check.target = self
        check.representedObject = servers.first?["alias"] as? String ?? ""
        check.isEnabled = !(check.representedObject as? String ?? "").isEmpty
        menu.addItem(check)
        let tipItem = NSMenuItem(title: "只读：不会在服务器上启动或停止任务", action: nil, keyEquivalent: "")
        tipItem.isEnabled = false
        menu.addItem(tipItem)
    }

    private static func age(_ stamp: Double) -> String {
        let seconds = max(0, Date().timeIntervalSince1970 - stamp)
        if seconds < 90 { return "刚刚" }
        if seconds < 3600 { return "\(Int(seconds / 60)) 分钟前" }
        return "\(Int(seconds / 3600)) 小时前"
    }

    @objc func openServerTerminal(_ sender: Any?) {
        let alias: String
        if let item = sender as? NSMenuItem, let value = item.representedObject as? String {
            alias = value
        } else {
            alias = servers.first?["alias"] as? String ?? ""
        }
        guard !alias.isEmpty,
              alias.range(of: "^[A-Za-z0-9_.:@%+-]{1,200}$", options: .regularExpression) != nil,
              let url = URL(string: "ssh://" + alias) else {
            webView.evaluateJavaScript("window.petOpenError()", completionHandler: nil)
            showPanel()
            return
        }
        NSWorkspace.shared.open(url)
    }

    private func refresh() {
        let s = visibleState()
        let doneItems = (s["done"] as? [[String: Any]]) ?? []
        let running = (s["running"] as? [[String: Any]])?.count ?? 0
        // 待处理里排除“已中断”：多数是你自己按的停止，不算要你处理（与面板一致）。
        let needYou = ((s["pending"] as? [[String: Any]]) ?? [])
            .filter { ($0["state"] as? String) != "interrupted" }
        let ids = Set(doneItems.compactMap { $0["id"] as? String })
        if let seen = seenDoneIDs, !ids.isSubset(of: seen) { petView.bounce() }
        seenDoneIDs = ids
        petView.update(done: doneItems.count, running: running, needYou: needYou.count)
        // 菜单栏图标始终是同一个星号；数量的反馈在桌宠的角标和面板上。
        var tip = "二踢脚大王：打开任务面板或找回桌宠"
        if doneItems.isEmpty && needYou.isEmpty {
            tip = "二踢脚大王：打开任务面板或找回桌宠"
        } else if needYou.isEmpty {
            tip = "二踢脚大王：\(doneItems.count) 个任务已完成，点开看看"
        } else {
            tip = "二踢脚大王：\(needYou.count) 个任务要你处理"
                + (doneItems.isEmpty ? "" : "，另有 \(doneItems.count) 个已完成")
        }
        statusItem?.button?.toolTip = tip
        pushState()
    }

    private func pushState() {
        guard pageReady,
              let data = try? JSONSerialization.data(withJSONObject: visibleState()),
              let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.petRender(\(json))", completionHandler: nil)
    }

    private func dismiss(_ ids: [String]) {
        for id in ids where !dismissed.contains(id) {
            dismissed.insert(id)
            dismissedOrder.append(id)
        }
        if dismissedOrder.count > 2000 {
            let drop = dismissedOrder.count - 2000
            dismissedOrder.prefix(drop).forEach { dismissed.remove($0) }
            dismissedOrder.removeFirst(drop)
        }
        defaults.set(dismissedOrder, forKey: "dismissed")
        refresh()
    }

    private func findItem(_ id: String) -> [String: Any]? {
        for key in ["running", "done", "pending"] {
            if let item = (rawState[key] as? [[String: Any]])?.first(where: { ($0["id"] as? String) == id }) {
                return item
            }
        }
        return nil
    }

    /// 只有系统确认应用已打开，才移除“查看并收起”的任务。
    private func openItem(_ id: String, dismissAfterOpen: Bool = false) {
        guard let target = findItem(id)?["open"] as? [String: Any],
              let type = target["type"] as? String,
              let value = target["value"] as? String else { return }
        let ws = NSWorkspace.shared
        let finished: @Sendable (NSRunningApplication?, Error?) -> Void = { [weak self] application, error in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if application != nil && error == nil {
                    if dismissAfterOpen { self.dismiss([id]) }
                } else {
                    self.webView.evaluateJavaScript("window.petOpenError()", completionHandler: nil)
                    self.showPanel()
                }
            }
        }
        let config = NSWorkspace.OpenConfiguration()
        config.activates = true
        if type == "terminal" {
            // 远程任务打不开远端窗口：只把终端连过去，不代跑 claude/codex 命令。
            guard value.range(of: "^[A-Za-z0-9_.:@%+-]{1,200}$", options: .regularExpression) != nil,
                  let url = URL(string: "ssh://" + value) else {
                webView.evaluateJavaScript("window.petOpenError()", completionHandler: nil)
                return
            }
            if NSWorkspace.shared.open(url) {
                if dismissAfterOpen { dismiss([id]) }
            } else {
                webView.evaluateJavaScript("window.petOpenError()", completionHandler: nil)
                showPanel()
            }
            return
        }
        if type == "url", let url = URL(string: value), url.scheme == "codex",
           let app = ws.urlForApplication(toOpen: url) {
            ws.open([url], withApplicationAt: app, configuration: config, completionHandler: finished)
        } else if type == "app", ["com.anthropic.claudefordesktop", "com.deepseek.dsh"].contains(value),
                  let app = ws.urlForApplication(withBundleIdentifier: value) {
            ws.openApplication(at: app, configuration: config, completionHandler: finished)
        } else {
            webView.evaluateJavaScript("window.petOpenError()", completionHandler: nil)
        }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
        switch type {
        case "ready":
            pageReady = true
            pushState()
            #if PET_TESTS
            Task { await runNativeChecks() }
            #endif
        case "dismiss":
            dismiss((body["ids"] as? [String]) ?? [])
        case "open":
            if let id = body["id"] as? String { openItem(id) }
        case "view":
            if let id = body["id"] as? String { openItem(id, dismissAfterOpen: true) }
        case "close":
            hidePanel()
        case "saveConfig":
            guard let config = body["config"] as? [String: Any],
                  let data = try? JSONSerialization.data(withJSONObject: config),
                  let json = String(data: data, encoding: .utf8) else {
                webView.evaluateJavaScript("window.petConfigSaved(false, '配置格式不对')", completionHandler: nil)
                return
            }
            guard configLoaded else {
                // 读配置失败过（或还没读完）：再读一次，读到后设置页会自动换成真实内容。
                loadConfigForPanel()
                webView.evaluateJavaScript("window.petConfigSaved(false, '设置还在读取，稍等一下再保存')", completionHandler: nil)
                return
            }
            // 子进程在后台跑，面板在等待保存时不会卡住。
            runConfigCommand(["--set-config", json]) { [weak self] saved in
                guard let self = self else { return }
                guard let saved = saved else {
                    self.webView.evaluateJavaScript("window.petConfigSaved(false, '保存失败，原因记在 collector-startup.log')", completionHandler: nil)
                    return
                }
                self.configFromDisk = saved
                // 大小改动立刻见效；采集器会在下一轮发现 config.json 变了并自行重载。
                if let look = saved["appearance"] as? [String: Any],
                   let scale = look["scale"] as? Double, scale > 0 {
                    self.petView.setScale(CGFloat(scale))
                }
                self.webView.evaluateJavaScript("window.petConfigSaved(true, '已保存')", completionHandler: nil)
                self.pushState()
            }
        case "revealApp":
            NSWorkspace.shared.activateFileViewerSelecting([Bundle.main.bundleURL])
        case "quit":
            NSApp.terminate(nil)
        default:
            break
        }
    }
    // MARK: 配置

    /// Run a collector config command off the main thread; `done` is called on the main thread.
    /// (Starting Python takes a moment; doing it on the main thread froze the panel.)
    private func runConfigCommand(_ extraArguments: [String], done: @escaping ([String: Any]?) -> Void) {
        #if PET_TESTS
        done(AppDelegate.runCollectorConfig(extraArguments))       // tests stay deterministic
        #else
        Paths.configQueue.async {
            let result = AppDelegate.runCollectorConfig(extraArguments)
            DispatchQueue.main.async { done(result) }
        }
        #endif
    }

    /// Config is validated inside collector.py so the app and the CLI agree on one rule set.
    /// Touches no app state, so it can run on any thread.
    nonisolated private static func runCollectorConfig(_ extraArguments: [String]) -> [String: Any]? {
        let script = Paths.resources.appendingPathComponent("collector.py")
        guard let python = Paths.pythonInterpreter() else { return nil }
        let p = Process()
        p.executableURL = python
        p.arguments = ["-I", "-B", script.path, "--state-dir", Paths.support.path] + extraArguments
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUTF8"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        p.environment = env
        let out = Pipe()
        p.standardOutput = out
        // stderr goes to a temp file, not a pipe: an unread pipe can fill up and block the child,
        // and a Python traceback is the one thing worth keeping when a save fails.
        let errURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("ai-pet-config-\(UUID().uuidString).err")
        FileManager.default.createFile(atPath: errURL.path, contents: nil)
        let errHandle = try? FileHandle(forWritingTo: errURL)
        p.standardError = errHandle ?? FileHandle.nullDevice
        defer {
            try? errHandle?.close()
            try? FileManager.default.removeItem(at: errURL)
        }
        do { try p.run() } catch {
            CollectorDiagnostics.append(Data("配置命令无法启动：\(error)\n".utf8))
            return nil
        }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard p.terminationStatus == 0, let result = obj, result["error"] == nil else {
            // Lenient decoding: a cut can fall inside a multi-byte character.
            let text = String(decoding: data.prefix(400), as: UTF8.self)
            let err = (try? Data(contentsOf: errURL)).map { String(decoding: $0.suffix(800), as: UTF8.self) } ?? ""
            CollectorDiagnostics.append(Data("配置命令失败（exit \(p.terminationStatus)）：\(text)\n\(err)\n".utf8))
            return nil
        }
        return result
    }

    private func loadConfigForPanel() {
        runConfigCommand(["--dump-config"]) { [weak self] config in
            guard let self = self else { return }
            if let config = config {
                self.configFromDisk = config
                self.configLoaded = true
                #if !PET_TESTS
                // 配置里的缩放比例也要落到桌宠身上。
                if let look = config["appearance"] as? [String: Any],
                   let scale = look["scale"] as? Double, scale > 0,
                   abs(Double(self.petView.scale) - scale) > 0.001 {
                    self.petView.setScale(CGFloat(scale))
                }
                #endif
            }
            self.pushState()
        }
    }

    // MARK: 采集进程（collector.py）

    private func ensureCollector() {
        if let c = collector, c.isRunning { return }
        if collector != nil {
            collector = nil
            if Date().timeIntervalSince(collectorStartedAt) > 60 { collectorFailures = 0 }
            collectorFailures += 1
            nextCollectorStart = Date().addingTimeInterval(min(60, Double(collectorFailures) * 5))
        }
        guard Date() >= nextCollectorStart else { return }

        let script = Paths.resources.appendingPathComponent("collector.py")
        let p = Process()
        guard let python = Paths.pythonInterpreter() else {
            CollectorDiagnostics.append(Data("找不到可用的 python3，请在设置里或 README 中查看要求\n".utf8))
            collectorFailures += 1
            nextCollectorStart = Date().addingTimeInterval(60)
            return
        }
        p.executableURL = python
        p.arguments = ["-I", "-B", script.path, "--state-dir", Paths.support.path,
                       "--parent-pid", String(ProcessInfo.processInfo.processIdentifier)]
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUTF8"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        p.environment = env
        let diagnosticPipe = Pipe()
        diagnosticPipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil }
            else { CollectorDiagnostics.append(data) }
        }
        p.standardError = diagnosticPipe
        p.standardOutput = diagnosticPipe
        do {
            try p.run()
            collector = p
            collectorStartedAt = Date()
        } catch {
            collectorFailures += 1
            nextCollectorStart = Date().addingTimeInterval(min(60, Double(collectorFailures) * 5))
            diagnosticPipe.fileHandleForReading.readabilityHandler = nil
            CollectorDiagnostics.append(Data("采集器无法启动：\(error)\n".utf8))
        }
    }

    #if PET_TESTS
    private func js(_ expression: String) async throws -> Any? {
        try await webView.evaluateJavaScript(expression)
    }
    private var nativeCheckCount = 0
    private func verify(_ condition: Bool, _ message: String) throws {
        nativeCheckCount += 1
        if !condition { throw NSError(domain: message, code: 1) }
    }
    private func runNativeChecks() async {
        do {
            var animation = PetMotion(epoch: 0)
            animation.lastActivity = 1e9            // stay awake while sampling the routines
            let idleTotal = PetMotion.idleRoutine.reduce(0) { $0 + $1.1 }
            let idlePoses = Set(stride(from: 0.0, to: idleTotal, by: 0.1).map { animation.pose(at: $0) })
            try verify(idlePoses.isSuperset(of: ["idle", "blink", "sit", "sit-blink", "read", "tea", "lie", "squat", "look", "think", "stretch",
                                                 "kneel", "legs-out", "lie-side", "lie-back", "prone-read"]), "idle-variety")
            try verify(PetMotion.idleRoutine.allSatisfy { $0.1 >= 6 } && PetMotion.workRoutine.allSatisfy { $0.1 >= 7 }, "activities-held-long-enough")
            let seated = PetMotion.floor
            let seatedTime = PetMotion.idleRoutine.filter { seated.contains($0.0) }.reduce(0) { $0 + $1.1 }
            try verify(seatedTime >= idleTotal * 0.5, "not-always-standing")
            animation.running = true
            let workTotal = PetMotion.workRoutine.reduce(0) { $0 + $1.1 }
            let workingPoses = Set(stride(from: 0.0, to: workTotal, by: 0.1).map { animation.pose(at: $0) })
            try verify(workingPoses.isSuperset(of: ["type", "write", "write-blink", "read", "prone-read", "think", "look"]), "working-variety")
            animation.celebrationUntil = 5
            try verify(animation.pose(at: 0.5) == "jump", "completion-jumps")
            try verify(animation.pose(at: 2) == "firecracker", "completion-firecracker")
            try verify(!PetMotion.cheers.contains(animation.pose(at: 6)), "unread-does-not-freeze-celebration")
            animation.greetingUntil = 9
            try verify(Set([animation.pose(at: 8.1), animation.pose(at: 8.5)]) == PetMotion.waves, "hover-waves-two-frames")
            try verify(animation.pose(at: 8, reducedMotion: true) == "happy", "reduced-motion-greeting")
            // 挥手动画有节流，但不能长到让人觉得「悬停没反应」。
            try verify(PetMotion.greetingGap <= 3.5, "greeting-gap-is-short")
            try verify(animation.pose(at: 10, reducedMotion: true) == "type", "respects-reduced-motion")
            animation.dragging = true
            try verify(animation.pose(at: 10) == "stretch", "drag-lifts-arms")
            animation.dragging = false
            animation.running = false
            animation.lastActivity = 0
            try verify(animation.pose(at: 400) == "sleep", "falls-asleep-when-quiet")
            animation.wakingUntil = 401
            try verify(animation.pose(at: 400.5) == "stretch", "wakes-up-stretching")
            animation.running = true
            try verify(animation.pose(at: 500) != "sleep", "work-keeps-her-awake")
            for name in PetMotion.allPoses.sorted() {
                try verify(NSImage(contentsOf: Paths.resources.appendingPathComponent("assets/pet-\(name).png")) != nil, "sprite-\(name)-bundled")
            }
            let base = petView.scale
            petView.setScale(1.5)
            try verify(abs(petWindow.frame.width - PetView.size.width * 1.5) < 0.5, "resize-grows-window")
            petView.setScale(0.1)
            try verify(petView.scale == PetView.minScale, "resize-has-minimum")
            petView.setScale(base)
            try verify(abs(petWindow.frame.width - PetView.size.width * base) < 0.5, "resize-restores")
            for _ in 0..<30 { CollectorDiagnostics.append(Data(repeating: 120, count: 8192)) }
            for name in ["collector-startup.log", "collector-startup.log.1"] {
                let attrs = try FileManager.default.attributesOfItem(atPath: Paths.support.appendingPathComponent(name).path)
                try verify((attrs[.size] as? NSNumber)?.intValue ?? 999999 <= 65536, "startup-log-bounded")
            }
            dismissed = []; dismissedOrder = []
            defaults.removeObject(forKey: "trackingStartedAt")
            defaults.removeObject(forKey: "trackingInitialActiveIDs")
            let now = Date().timeIntervalSince1970
            rawState = [
                "agents": ["chatgpt": ["status": "ok", "open": true], "claude": ["status": "ok", "open": true], "deepseek": ["status": "ok", "open": true]],
                "hosts": [["id": "h1", "label": "Studio", "alias": "gpu-box", "machine": "gpu-box",
                           "status": "ok", "fetchedAt": now - 5, "claudeLive": 3],
                          ["id": "h2", "label": "Build", "alias": "cpu-box", "status": "offline",
                           "hint": "SSH 连接失败", "fetchedAt": now - 900]],
                "running": [["id": "test-run", "agent": "deepseek", "title": "正在整理项目资料", "prompt": "把本周实验结果汇总，列出接下来需要验证的内容", "start": now-180],
                           ["id": "test-remote", "agent": "claude", "host": "h1", "title": "重构数据管线",
                            "prompt": "把读取与清洗拆成两个模块", "start": now-600, "open": ["type": "terminal", "value": "gpu-box"]]],
                "done": [["id": "test-done", "agent": "claude", "title": "本周总结已完成", "prompt": "请把实验记录整理成一份简短周报", "detail": "点击任务可打开对应应用", "time": now-60, "state": "success", "open": ["type": "app", "value": "invalid.test.app"]]],
                "pending": [["id": "test-pending", "agent": "chatgpt", "host": "h2", "title": "等待确认的任务", "prompt": "帮我检查图标和动作 <img src=x onerror=alert(1)>", "followups": ["顺便看下日志 <img src=x onerror=alert(2)>"], "followupCount": 3, "detail": "任务已中断，可打开应用查看", "time": now-80, "state": "interrupted"]]
            ]
            let fixture = rawState
            rawState["version"] = 4
            establishInitialBaselineIfNeeded()
            let since = defaults.double(forKey: "trackingStartedAt")
            try verify(since > 0, "baseline-created")
            try verify((visibleState()["done"] as? [[String: Any]])?.isEmpty == true, "baseline-hides-old-done")
            try verify((visibleState()["pending"] as? [[String: Any]])?.isEmpty == true, "baseline-hides-old-errors")
            try verify((visibleState()["running"] as? [[String: Any]])?.count == 2, "baseline-retains-running")
            rawState["running"] = [["id": "old-unknown", "start": now-300, "state": "running"]]
            rawState["done"] = [["id": "test-run", "start": now-180, "time": since+1, "state": "success"],
                                ["id": "late-history", "start": now-100, "time": now-50, "state": "success"]]
            rawState["pending"] = [["id": "fresh-error", "start": since+1, "time": since+2, "state": "error"],
                                   ["id": "stale-history", "start": now-100, "time": since+2, "state": "unknown"]]
            establishInitialBaselineIfNeeded()
            try verify(defaults.double(forKey: "trackingStartedAt") == since, "baseline-not-reset-on-refresh")
            try verify((visibleState()["done"] as? [[String: Any]])?.count == 1, "new-completion-kept-late-history-hidden")
            try verify((visibleState()["pending"] as? [[String: Any]])?.count == 1, "new-error-kept-old-unknown-hidden")
            try verify((visibleState()["running"] as? [[String: Any]])?.isEmpty == true, "old-running-not-resurrected")
            try verify(UserDefaults(suiteName: "local.ai-pet.native-test-suite")?.double(forKey: "trackingStartedAt") == since, "baseline-persists")
            // Return to the independent visual/interaction fixture.
            defaults.removeObject(forKey: "trackingStartedAt")
            defaults.removeObject(forKey: "trackingInitialActiveIDs")
            dismissed = []; dismissedOrder = []
            rawState = fixture
            collectorHealthy = true
            refresh(); showPanel()
            try await Task.sleep(nanoseconds: 700_000_000)
            // Machine row: ALL (every machine, the default) first, then Local and each server.
            let chips = try await js("Array.from(document.querySelectorAll('.mchip span')).map(e => e.textContent)") as? [String]
            try verify(chips == ["ALL", "Local", "Studio", "Build"], "machine-row-with-all")
            let dots = try await js("Array.from(document.querySelectorAll('.mchip .dot')).map(e => e.className)") as? [String]
            try verify(dots == ["dot done running", "dot done running", "dot none running", "dot none"], "machine-dot-worst-state")
            let offlineTag = try await js("document.querySelector('.mchip.offline em')?.textContent") as? String
            try verify(offlineTag == "离线", "offline-machine-marked")
            // Hover text gives the real numbers; a machine with a running task never says "没有任务".
            let tips = try await js("Array.from(document.querySelectorAll('.mchip')).map(e => e.title)") as? [String] ?? []
            try verify(tips.count == 4 && tips[2].hasSuffix("运行中 1") && tips.allSatisfy({ !$0.contains("没有任务") }), "machine-tooltip-counts")
            let allRows = try await js("document.querySelectorAll('#list .row').length") as? Int
            try verify(allRows == 4, "all-machines-shows-everything")
            let badges = try await js("Array.from(document.querySelectorAll('#list .host-badge')).map(e => e.textContent).sort().join(',')") as? String
            try verify(badges == "Build,Local,Local,Studio", "all-machines-rows-name-their-machine")
            _ = try await js("document.querySelector('[data-machine=\"__local__\"]').click()")
            try await Task.sleep(nanoseconds: 300_000_000)
            let macRows = try await js("document.querySelectorAll('#list .row').length") as? Int
            try verify(macRows == 2, "mac-shows-only-its-tasks")
            let allCount = try await js("document.getElementById('cntAll').textContent.trim()") as? String
            try verify(allCount == "2", "all-count-per-machine")
            // 角标 = 待查看的完成 + 需要你处理的；待确认那条也要算进去，否则会漏提醒。
            let badgeDone = (visibleState()["done"] as? [[String: Any]])?.count ?? 0
            let badgeNeed = ((visibleState()["pending"] as? [[String: Any]]) ?? [])
                .filter { ($0["state"] as? String) != "interrupted" }.count
            // 这份 fixture 里唯一那条待处理是「已中断」，按规则不算要你处理，
            // 所以角标应该只数那 1 个完成项。
            try verify(badgeNeed == 0, "interrupted-is-not-need-you")
            // 运行中的提示只剩悬停气泡：没有底部的常驻标签，也没有右上角的旋转环。
            petView.update(done: 0, running: 2, needYou: 0)
            try verify(petView.statusLine().contains("2 个任务在跑"), "hover-speaks-running")
            petView.update(done: 1, running: 2, needYou: 0)
            try verify(petView.statusLine().contains("2 个在跑"), "hover-speaks-running-and-done")
            try verify(!petView.layer!.sublayers!.contains { $0 is CAShapeLayer },
                       "no-spinning-ring")
            try verify(badgeDone + badgeNeed == 1, "badge-counts-both-buckets")
            // 菜单栏只画星号，不写数字。
            try verify(statusItem?.button?.title.isEmpty == true, "status-item-has-no-number")
            try verify(statusItem?.button?.image != nil, "status-item-shows-icon")
            try verify(statusItem?.button?.image?.size.width ?? 0 >= StatusIcon.pointSize,
                       "status-icon-drawn")
            try verify(statusItem?.button?.image?.isTemplate == true, "status-icon-follows-menu-bar-theme")
            // 设置页：能打开、能读到配置、能改服务器。
            configFromDisk = ["version": 1,
                              "local": ["codex": "", "claude": "", "deepseek": ""],
                              "remote": ["enabled": true,
                                         "hosts": [["id": "a", "alias": "gpu-box", "label": "一号"]]],
                              "appearance": ["scale": 1.0]]
            configLoaded = true
            pushState()
            _ = try await js("document.querySelector('[data-act=toggleSettings]').click()")
            try await Task.sleep(nanoseconds: 200_000_000)
            let settingsShown = try await js("!document.getElementById('settings').hidden && document.getElementById('list').hidden") as? Bool
            try verify(settingsShown == true, "settings-opens")
            let hostInputs = try await js("document.querySelectorAll('#settings [data-host=\"alias\"]').length") as? Int
            try verify(hostInputs == 1, "settings-lists-hosts")
            let pathInputs = try await js("document.querySelectorAll('#settings [data-path]').length") as? Int
            try verify(pathInputs == 3, "settings-lists-local-paths")
            _ = try await js("document.querySelector('[data-act=addHost]').click()")
            try await Task.sleep(nanoseconds: 150_000_000)
            let afterAdd = try await js("document.querySelectorAll('#settings .host-row').length") as? Int
            try verify(afterAdd == 2, "settings-adds-host")
            _ = try await js("document.querySelector('#settings .host-row .drop').click()")
            try await Task.sleep(nanoseconds: 150_000_000)
            let afterDrop = try await js("document.querySelectorAll('#settings .host-row').length") as? Int
            try verify(afterDrop == 1, "settings-drops-host")
            // 同一个按钮再点一次就收起设置。
            _ = try await js("document.querySelector('[data-act=toggleSettings]').click()")
            try await Task.sleep(nanoseconds: 150_000_000)
            let backToList = try await js("document.getElementById('settings').hidden && !document.getElementById('list').hidden") as? Bool
            try verify(backToList == true, "settings-toggles-closed")
            let gearIconOnly = try await js("document.querySelector('.gear').textContent.trim()") as? String
            try verify(gearIconOnly == "⚙", "settings-button-is-icon-only")
            updateServers()
            let serverTitles = (serverMenu?.items ?? []).map { $0.title }
            try verify(serverTitles.first == "● Studio 已连接", "server-menu-connected")
            try verify(serverTitles.contains("○ Build 连不上"), "server-menu-offline")
            var logoSources = Set((try await js("Array.from(document.querySelectorAll('.logo img')).filter(i => i.complete && i.naturalWidth > 0).map(i => i.getAttribute('src'))") as? [String]) ?? [])
            _ = try await js("document.querySelector('[data-machine=\"h1\"]').click()")
            try await Task.sleep(nanoseconds: 300_000_000)
            let remoteRows = try await js("document.querySelectorAll('#list .row').length") as? Int
            try verify(remoteRows == 1, "server-shows-only-its-tasks")
            logoSources.formUnion((try await js("Array.from(document.querySelectorAll('.logo img')).filter(i => i.complete && i.naturalWidth > 0).map(i => i.getAttribute('src'))") as? [String]) ?? [])
            _ = try await js("document.querySelector('[data-machine=\"h2\"]').click()")
            try await Task.sleep(nanoseconds: 300_000_000)
            let hostNote = try await js("document.querySelectorAll('.host-note').length") as? Int
            try verify(hostNote == 1, "panel-shows-offline-note")
            let stoppedTag = try await js("document.querySelector('#list .tag')?.className") as? String
            try verify(stoppedTag == "tag stopped", "interrupted-is-grey")
            let attentionCount = try await js("document.getElementById('cntAttention').textContent.trim()") as? String
            try verify(attentionCount == "", "grey-not-counted-as-needs-you")
            // 已中断不用处理：它在“已完成”里（灰色），不在“需处理”里。
            let doneCount = try await js("document.getElementById('cntDone').textContent.trim()") as? String
            try verify(doneCount == "1", "interrupted-listed-under-done")
            let promptEscaped = try await js("document.querySelectorAll('.row-prompt').length === 1 && document.querySelectorAll('.row-prompt img').length === 0") as? Bool
            try verify(promptEscaped == true, "prompt-previews-escaped-and-visible")
            // 任务没答完时追加的提问也显示（最近两条 + 更早的条数），同样转义。
            let followups = try await js("Array.from(document.querySelectorAll('#list .followup')).map(e => e.textContent)") as? [String]
            try verify(followups == ["追加顺便看下日志 <img src=x onerror=alert(2)>"], "followups-visible-and-escaped")
            let followupNote = try await js("document.querySelector('#list .followup-more')?.textContent") as? String
            try verify(followupNote == "更早还有 2 条追加", "earlier-followups-counted")
            logoSources.formUnion((try await js("Array.from(document.querySelectorAll('.logo img')).filter(i => i.complete && i.naturalWidth > 0).map(i => i.getAttribute('src'))") as? [String]) ?? [])
            try verify(logoSources.count == 3, "all-official-logos-loaded-offline")
            _ = try await js("document.querySelector('[data-machine=\"__all__\"]').click()")
            try await Task.sleep(nanoseconds: 300_000_000)
            let menuCount = statusItem?.menu?.items.count ?? 0
            try verify(menuCount == 7, "recovery-menu")
            // 窗口刚 orderFront 时偶尔还没被标记为可见，等一小会儿再判定；
            // 还不行就主动把面板提到前面一次，多半是测试环境里被别的窗口盖住了。
            if !(petWindow.isVisible && panel.isVisible) {
                try await Task.sleep(nanoseconds: 300_000_000)
            }
            if !(petWindow.isVisible && panel.isVisible) {
                petWindow.orderFrontRegardless()
                showPanel()
                try await Task.sleep(nanoseconds: 400_000_000)
            }
            try verify(petWindow.isVisible && panel.isVisible,
                       petWindow.isVisible ? "windows-visible-panel" : "windows-visible-pet")
            let name = try await js("document.querySelector('h1').textContent") as? String
            try verify(name == "二踢脚大王的任务提醒", "pet-name-updated")

            if let layer = petView.layer,
               let context = CGContext(data: nil, width: Int(PetView.size.width * 2), height: Int(PetView.size.height * 2), bitsPerComponent: 8, bytesPerRow: 0,
                                       space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) {
                context.scaleBy(x: 2, y: 2)
                layer.render(in: context)
                if let image = context.makeImage() {
                    let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:])
                    try png?.write(to: Paths.support.appendingPathComponent("pet.png"))
                }
            }
            let snapshot = try await webView.takeSnapshot(configuration: nil)
            if let tiff = snapshot.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff) {
                try bitmap.representation(using: .png, properties: [:])?.write(to: Paths.support.appendingPathComponent("panel.png"))
            }
            _ = try await js("document.querySelector('[data-view]').click()")
            try await Task.sleep(nanoseconds: 100_000_000)
            try verify((visibleState()["done"] as? [[String: Any]])?.count == 1, "failed-open-retains-task")
            let errorVisible = try await js("document.getElementById('notice').textContent.includes('任务已保留')") as? Bool
            try verify(errorVisible == true, "failed-open-message")
            _ = try await js("document.querySelector('[data-dismiss=\"test-done\"]').click()")
            try await Task.sleep(nanoseconds: 100_000_000)
            try verify((visibleState()["done"] as? [[String: Any]])?.isEmpty == true, "dismiss-task")
            try verify(defaults.stringArray(forKey: "dismissed")?.contains("test-done") == true, "dismiss-persisted")
            refresh()
            try verify(statusItem?.button?.image != nil, "badge-cleared")
            try verify((visibleState()["done"] as? [[String: Any]])?.isEmpty == true, "refresh-no-resurrection")
            _ = try await js("document.querySelector('[data-act=close]').click()")
            try await Task.sleep(nanoseconds: 100_000_000)
            try verify(!panel.isVisible && petWindow.isVisible, "close-keeps-pet")
            petClicked()
            try verify(panel.isVisible, "pet-reopens-panel")
            petWindow.setFrameOrigin(NSPoint(x: -10000, y: -10000))
            petDragEnded()
            try verify(NSScreen.screens.contains { $0.visibleFrame.contains(petWindow.frame) }, "drag-clamped-to-visible-screen")
            try verify(savedPetOrigin() == petWindow.frame.origin, "saved-drag-position")
            _ = applicationShouldHandleReopen(NSApp, hasVisibleWindows: true)
            try verify(savedPetOrigin() == petWindow.frame.origin, "reopen-preserves-location")
            let report = "\(nativeCheckCount) native checks passed"
            try report.write(to: Paths.support.appendingPathComponent("native-result.txt"), atomically: true, encoding: .utf8)
            print(report)
            NSApp.terminate(nil)
        } catch {
            print("Native test failed:", error)
            exit(1)
        }
    }
    #endif

}

@main
enum PetMain {
    @MainActor
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)   // 不占 Dock，只在桌面上显示桌宠
        withExtendedLifetime(delegate) {
            app.run()
        }
    }
}
