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

/// Animation time is independent of data refreshes; unread counts never freeze a pose.
/// Each activity is held for tens of seconds; only blinking and waving flip frames quickly.
/// 九套衣服。每天自动换一套：九天一个回合，每回合随机打乱一次顺序，回合内九天九套各不相同；
/// 也可以在菜单「衣服」里固定一套。
/// 每套是 Resources/assets/outfits/<名字>/pet-*.png；缺了就退回 assets/ 下那套（月下青竹）。
enum Outfits {
    static let order = ["月下青竹", "琥珀节拍", "白衫小调", "玫瑰暗夜", "雾中蝶影", "松间素影", "雪夜归途", "银杏街角", "星光夜曲"]
    static let fallback = "月下青竹"

    /// 今天穿哪套：从 2026-01-01 起按本地日期数天数；每 n 天一个回合，回合内按该回合打乱后的顺序穿。
    static func daily(from available: [String], on date: Date = Date(), calendar: Calendar = .current) -> String {
        guard !available.isEmpty else { return fallback }
        var origin = DateComponents()
        (origin.year, origin.month, origin.day) = (2026, 1, 1)
        let start = calendar.date(from: origin) ?? Date(timeIntervalSince1970: 0)
        let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: start), to: calendar.startOfDay(for: date)).day ?? 0
        let n = available.count
        let round = days >= 0 ? days / n : (days - n + 1) / n          // floor division
        return order(forRound: round, from: available)[days - round * n]
    }

    /// 第 round 个回合的穿衣顺序：随机打乱，回合内各不相同；新回合第一天不和上一回合最后一天撞衫。
    /// 由回合号决定，不存任何状态：同一天无论何时打开都是同一套。
    static func order(forRound round: Int, from available: [String]) -> [String] {
        var list = shuffled(available, seed: round)
        // swapping the first two never moves the last one, so the previous round's last day is its raw last
        if list.count > 2, list[0] == shuffled(available, seed: round - 1).last {
            list.swapAt(0, 1)
        }
        return list
    }

    /// Fisher-Yates with SplitMix64: the same order on every Mac and every Swift version.
    private static func shuffled(_ items: [String], seed: Int) -> [String] {
        var state = UInt64(bitPattern: Int64(seed)) &* 0x9E3779B97F4A7C15 &+ 0x2545F4914F6CDD1D
        func next() -> UInt64 {
            state &+= 0x9E3779B97F4A7C15
            var z = state
            z = (z ^ (z >> 30)) &* 0xBF58476D1CE4E5B9
            z = (z ^ (z >> 27)) &* 0x94D049BB133111EB
            return z ^ (z >> 31)
        }
        var list = items
        var i = list.count - 1
        while i > 0 {
            list.swapAt(i, Int(next() % UInt64(i + 1)))
            i -= 1
        }
        return list
    }
}

struct PetMotion {
    var running = false
    var celebrationUntil: TimeInterval = 0
    var greetingUntil: TimeInterval = 0
    var wakingUntil: TimeInterval = 0
    var lastActivity: TimeInterval = 0
    var epoch: TimeInterval = 0
    var dragging = false
    /// 退出前说再见：在这之前一直显示 bye。
    var leavingUntil: TimeInterval = 0

    /// 任务完成的庆祝三段：跳起来 → 举二踢脚 → 笑着收尾。
    static let celebrationBeats: [(String, TimeInterval)] = [("jump", 1.4), ("firecracker", 3.0), ("happy", 2.1)]
    static var celebration: TimeInterval { celebrationBeats.reduce(0) { $0 + $1.1 } }
    static let sleepAfter: TimeInterval = 300          // 5 minutes with nothing happening
    /// 悬停打招呼：手抬到肩侧轻轻挥一下，浅笑。一帧，靠身体轻摆表现挥手。
    /// （以前是手臂前伸、掌心朝人的 present 两帧，看起来像在挡人，换掉了。）
    static let greeting = "wave"
    /// 挥手动画的最短间隔。气泡不受它限制：每次真的移上来都要能看到状态。
    static let greetingGap: TimeInterval = 3
    /// 退出前 bye 停留的时长。
    static let farewell: TimeInterval = 1.4
    /// Sitting (on the stool or the floor), kneeling or lying: barely sway; not "standing".
    static let floor: Set<String> = ["sit", "sit-blink", "edge-sit", "edge-sit-blink", "chin-rest", "guitar", "daze",
                                     "read", "write", "write-blink", "tea", "type", "kneel", "legs-out",
                                     "lie", "prone-read", "lie-side", "sleep"]
    static let cheers: Set<String> = ["happy", "jump", "firecracker"]
    /// Idle routine: (activity, seconds). Most of it is sitting or lying, not standing.
    static let idleRoutine: [(String, TimeInterval)] = [
        ("idle", 25), ("edge-sit", 35), ("chin-rest", 30), ("legs-out", 30), ("lie", 40), ("guitar", 40),
        ("look", 8), ("sit", 35), ("tea", 35), ("prone-read", 40), ("daze", 30), ("kneel", 30),
        ("lie-side", 40), ("idle", 15), ("stretch", 6),
    ]
    /// While a task runs she works: typing, writing, reading, thinking, and her stage habits in between.
    static let workRoutine: [(String, TimeInterval)] = [
        ("type", 45), ("stance", 10), ("write", 35), ("arms-crossed", 10), ("read", 30), ("think", 9),
        ("type", 40), ("drumstick", 12), ("write", 30), ("ending", 8),
    ]
    /// Every sprite the schedule can show.
    static var allPoses: Set<String> {
        Set((idleRoutine + workRoutine).map { $0.0 })
            .union(["blink", "sit-blink", "write-blink", "edge-sit-blink", "sleep", "happy", "jump", "firecracker",
                    greeting, "bye", "drag"])
    }

    /// Frames that belong together (blinks); switching inside a pair is instant, not a crossfade.
    static func base(_ pose: String) -> String {
        switch pose {
        case "blink": return "idle"
        case "sit-blink": return "sit"
        case "write-blink": return "write"
        case "edge-sit-blink": return "edge-sit"
        default: return pose
        }
    }

    func asleep(at now: TimeInterval) -> Bool {
        !running && !dragging && now >= celebrationUntil && now >= greetingUntil && now >= wakingUntil
            && now - lastActivity > Self.sleepAfter
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
        if now < leavingUntil { return "bye" }               // quitting: a still pose, also with reduced motion
        if reducedMotion {
            if now < celebrationUntil || now < greetingUntil { return "happy" }
            return running ? "type" : "idle"
        }
        if dragging { return "drag" }                        // lifted by the shoulders, legs dangling
        if now < celebrationUntil {
            var t = Self.celebration - (celebrationUntil - now)      // seconds since the celebration started
            for (beat, seconds) in Self.celebrationBeats {
                if t < seconds { return beat }
                t -= seconds
            }
            return Self.celebrationBeats.last?.0 ?? "happy"
        }
        if now < wakingUntil { return "stretch" }            // just woken up
        if now < greetingUntil { return Self.greeting }
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
        case "edge-sit": return blink ? "edge-sit-blink" : "edge-sit"
        default: return current
        }
    }
}

/// 桌宠本体：透明背景的 8 头身全身角色、完成数角标、运行中提示，支持拖动和点击。
final class PetView: NSView {
    static let size = NSSize(width: 300, height: 344)   // base layout size; the top 26 pt hold the speech bubble
    /// Sprites are 750×850 px (scripts/prepare_big_sprites.py), shown at 2.5 px per pt.
    static let avatarSize = NSSize(width: 300, height: 340)
    /// In the sprite, from its bottom edge: the feet (y 828 of 850) and the top of the standing head (y 128).
    static let footFraction: CGFloat = (850 - 828) / 850.0
    static let crownFraction: CGFloat = (850 - 128) / 850.0
    static let minScale: CGFloat = 0.6
    static let maxScale: CGFloat = 1.8
    static let presets: [(String, CGFloat)] = [("小", 0.75), ("中（默认）", 1.0), ("大", 1.3), ("特大", 1.6)]

    /// Display scale of the whole pet (sprite, bubble). The window follows it.
    private(set) var scale: CGFloat = 1
    private let stage = CALayer()       // holds every pet layer at base size; scaled as one

    weak var owner: AppDelegate?

    private let body = CALayer()        // 上下漂浮
    private let content = CALayer()     // 完成时弹一下
    private let plate = CALayer()
    private let face = CALayer()
    /// Sprites are decoded when first shown and only a few are kept: a decoded 750×850 sprite is 2.5 MB,
    /// a whole outfit would be ~85 MB. Poses change every few tens of seconds, so decoding is rare.
    private var loadSprite: (String) -> CGImage?
    private var spriteCache: [String: CGImage] = [:]
    private var spriteOrder: [String] = []
    static let spriteCacheLimit = 8

    private func sprite(_ pose: String) -> CGImage? {
        if let hit = spriteCache[pose] {
            spriteOrder.removeAll { $0 == pose }
            spriteOrder.append(pose)
            return hit
        }
        guard let image = loadSprite(pose) else { return pose == "idle" ? nil : sprite("idle") }
        spriteCache[pose] = image
        spriteOrder.append(pose)
        while spriteOrder.count > Self.spriteCacheLimit {
            spriteCache.removeValue(forKey: spriteOrder.removeFirst())
        }
        return image
    }
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
        CGRect(x: (PetView.size.width - PetView.avatarSize.width) / 2, y: 4,
               width: PetView.avatarSize.width, height: PetView.avatarSize.height)
    }

    /// 站着的她在窗口里左右占到哪里（窗口宽度的比例）。窗口两边各有 55–110 pt 的透明区，
    /// 拖到屏幕边上时按这里限制位置，人才能贴到屏幕边，而不是透明窗口贴边、人离边一大截。
    /// 用 idle 量：别的姿势大多只比它宽一点；跳起举二踢脚那几秒会多伸出去一些，伸出屏幕也没关系。
    private(set) var bodySpan: (min: CGFloat, max: CGFloat) = (min: 0, max: 1)

    /// 图里不透明的部分左右占到哪里（0…1）。和 scripts/prepare_big_sprites.py 一样用 alpha ≥ 128，
    /// 不把接地影子算进去。
    static func opaqueSpan(_ image: CGImage) -> (min: CGFloat, max: CGFloat)? {
        let w = image.width, h = image.height
        guard w > 0, h > 0,
              let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue),
              let data = ctx.data else { return nil }
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
        let rowBytes = ctx.bytesPerRow
        let px = data.bindMemory(to: UInt8.self, capacity: rowBytes * h)
        var lo = w, hi = -1
        for y in 0..<h {
            let row = y * rowBytes
            var x = 0
            while x < lo { if px[row + x * 4 + 3] >= 128 { lo = x; break }; x += 1 }
            x = w - 1
            while x > hi { if px[row + x * 4 + 3] >= 128 { hi = x; break }; x -= 1 }
        }
        guard hi >= lo else { return nil }
        return (min: CGFloat(lo) / CGFloat(w), max: CGFloat(hi + 1) / CGFloat(w))
    }

    /// 图的比例换算到窗口的比例（图按 resizeAspect 画在 avatarFrame 里）。
    private func updateBodySpan(_ idle: CGImage?) {
        guard let idle = idle, let s = PetView.opaqueSpan(idle) else { bodySpan = (min: 0, max: 1); return }
        let a = avatarFrame
        let drawn = CGFloat(idle.width) * min(a.width / CGFloat(idle.width), a.height / CGFloat(idle.height))
        let left = a.midX - drawn / 2
        bodySpan = (min: (left + s.min * drawn) / PetView.size.width,
                    max: (left + s.max * drawn) / PetView.size.width)
    }

    init(loader: @escaping (String) -> CGImage?) {
        loadSprite = loader
        super.init(frame: NSRect(origin: .zero, size: PetView.size))
        let root = CALayer()
        layer = root            // 自己管理图层树
        wantsLayer = true
        let idle = sprite("idle")
        updateBodySpan(idle)
        buildLayers(root: root, image: idle)
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
        // Grounded sprites have their contact shadow baked in. This one is the floor shadow under a jump:
        // it stays on the floor while she is in the air, so it is only shown for that pose.
        plate.frame = CGRect(x: a.midX - 40, y: a.minY + a.height * PetView.footFraction - 4, width: 80, height: 8)
        plate.cornerRadius = 4
        plate.isHidden = true
        plate.backgroundColor = NSColor(calibratedWhite: 0.1, alpha: 0.08).cgColor
        plate.shadowColor = NSColor.black.cgColor
        plate.shadowOpacity = 0.2
        plate.shadowRadius = 5
        plate.shadowOffset = CGSize(width: 0, height: -1)
        plate.shadowPath = CGPath(ellipseIn: plate.bounds, transform: nil)
        content.addSublayer(plate)

        face.frame = a
        face.anchorPoint = CGPoint(x: 0.5, y: PetView.footFraction)      // sway and squash pivot at the feet
        face.position = CGPoint(x: a.midX, y: a.minY + a.height * PetView.footFraction)
        face.cornerRadius = 0
        face.masksToBounds = false
        face.backgroundColor = NSColor.clear.cgColor
        face.contentsGravity = .resizeAspect
        face.contents = image
        face.contentsScale = scale
        content.addSublayer(face)


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
        var w = min(PetView.size.width - 8, textWidth(text, size: size) + 20)
        // 气泡在人头顶居中。她贴着屏幕边时窗口有一部分在屏幕外：气泡挪到窗口在屏幕里的那一段，
        // 放不下就缩窄（字尾变成 …），别被屏幕边切掉。
        var lo: CGFloat = 4
        var hi: CGFloat = PetView.size.width - 4          // 气泡能放的范围（窗口里，基础大小）
        if let win = window, let vf = (win.screen ?? NSScreen.main)?.visibleFrame, scale > 0 {
            lo = max(lo, (vf.minX + 4 - win.frame.minX) / scale)
            hi = min(hi, (vf.maxX - 4 - win.frame.minX) / scale)
        }
        if hi - lo >= 40 {
            w = min(w, hi - lo)
        } else {                                           // 窗口几乎全在屏幕外（拖动途中）：照旧居中
            lo = 4
            hi = PetView.size.width - 4
        }
        let x = min(max((PetView.size.width - w) / 2, lo), hi - w)
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        bubble.frame = CGRect(x: x, y: PetView.size.height - h - 3, width: w, height: h)
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
            face.contents = sprite(pose)
            plate.isHidden = pose != "jump"
            CATransaction.commit()
        }
        guard !reduced else { return }
        // One sway per pose group, so blink frames don't restart it.
        let group: String
        if motion.dragging { group = "drag" }
        else if pose == PetMotion.greeting { group = "greet" }
        else if PetMotion.cheers.contains(pose) { group = "cheer" }
        else if pose.hasPrefix("write") { group = "write" }
        else if pose == "sleep" { group = "sleep" }
        else if PetMotion.floor.contains(pose) { group = "seated" }
        else { group = "calm" }
        guard group != currentSway else { return }
        currentSway = group
        face.removeAnimation(forKey: "gesture")
        face.removeAnimation(forKey: "dangle")
        if group == "drag" { dangle(); return }
        // She is ~1.75× as tall as the old chibi, so the same angle would swing her head that much further.
        var angle = 0.013, duration = 3.6
        switch group {
        case "greet": (angle, duration) = (0.012, 1.5)    // one gentle side-to-side sway with the wave
        case "cheer": (angle, duration) = (0.026, 0.85)
        case "write": (angle, duration) = (0.007, 3.6)
        case "sleep": (angle, duration) = (0.012, 6.0)    // slow breathing-like nod while asleep
        case "seated": (angle, duration) = (0.005, 5.0)   // sitting/lying: barely moves
        default: break
        }
        let sway = CAKeyframeAnimation(keyPath: "transform.rotation.z")
        sway.values = [0, angle, 0, -angle, 0]
        sway.duration = duration
        sway.repeatCount = .infinity
        sway.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        face.add(sway, forKey: "gesture")
    }

    /// Carried by the shoulders: swing like a pendulum hanging from the top of the figure. The layer pivots
    /// at the feet, so a sideways shift that cancels the rotation at shoulder height keeps that point still.
    private func dangle() {
        let angle = 0.05, duration = 1.1
        let arm = Double(face.bounds.height) * Double(PetView.crownFraction - PetView.footFraction) * 0.9
        let swing = CAKeyframeAnimation(keyPath: "transform.rotation.z")
        swing.values = [0, angle, 0, -angle, 0]
        let shift = CAKeyframeAnimation(keyPath: "transform.translation.x")
        shift.values = [0, arm * angle, 0, -arm * angle, 0]
        let group = CAAnimationGroup()
        group.animations = [swing, shift]
        group.duration = duration
        group.repeatCount = .infinity
        group.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        face.add(group, forKey: "dangle")
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
        breathing.toValue = 1.009              // taller figure: the same 2-3 pt rise at the head as before
        breathing.duration = 1.8
        breathing.autoreverses = true
        breathing.repeatCount = .infinity
        breathing.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        face.add(breathing, forKey: "breathing")
    }

    #if PET_TESTS
    var posePresented: String { currentPose }
    var spriteShown: CGImage? { face.contents.map { $0 as! CGImage } }
    var spritesDecoded: Int { spriteCache.count }
    #endif

    /// 换衣服：换掉整套 sprite，当前姿势立刻用新衣服显示（淡入）。只保留当前这一套，省内存。
    func setOutfit(loader: @escaping (String) -> CGImage?) {
        guard let idle = loader("idle") else { return }
        loadSprite = loader
        spriteCache = ["idle": idle]
        spriteOrder = ["idle"]
        updateBodySpan(idle)
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        if !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            let fade = CATransition()
            fade.type = .fade
            fade.duration = 0.5
            face.add(fade, forKey: "poseFade")
        }
        face.contents = sprite(currentPose)
        CATransaction.commit()
    }

    /// 退出前：双手指尖捏合说再见。
    func sayGoodbye() {
        motion.leavingUntil = CACurrentMediaTime() + PetMotion.farewell + 1
        say(["下次见～", "我先走啦", "拜拜"].randomElement() ?? "", seconds: PetMotion.farewell + 1)
        animate()
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
        a.values = [1.0, 1.06, 0.97, 1.02, 1.0]   // smaller pop: at 1.12 her raised arms would leave the window
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
        for l in [face, bubbleText, bubble, plate] as [CALayer] { l.contentsScale = s }
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
    private var outfitItems: [NSMenuItem] = []
    private(set) var outfit = ""
    /// 应用里有的几套（按 Outfits.order）。启动时扫一次。
    private lazy var availableOutfits: [String] = {
        let root = Paths.resources.appendingPathComponent("assets/outfits")
        let found = Outfits.order.filter { name in
            ["webp", "png"].contains { ext in FileManager.default.fileExists(atPath: root.appendingPathComponent("\(name)/pet-idle.\(ext)").path) }
        }
        return found.isEmpty ? [Outfits.fallback] : found
    }()

    /// 固定的那套（defaults 里的 outfit），没有或已不存在就按日期轮换。
    private var chosenOutfit: String {
        let pinned = defaults.string(forKey: "outfit") ?? ""
        return availableOutfits.contains(pinned) ? pinned : Outfits.daily(from: availableOutfits)
    }

    /// The file for one pose of one outfit: outfits are bundled as lossless WebP (PNG still accepted);
    /// a missing pose falls back to the default outfit.
    func spriteURL(_ pose: String, outfit name: String) -> URL? {
        let assets = Paths.resources.appendingPathComponent("assets")
        var candidates: [URL] = []
        for set in [name, Outfits.fallback] {
            for ext in ["webp", "png"] {
                candidates.append(assets.appendingPathComponent("outfits/\(set)/pet-\(pose).\(ext)"))
            }
        }
        return candidates.first { FileManager.default.fileExists(atPath: $0.path) }
    }

    /// Decodes one sprite straight to a CGImage (no NSImage keeping a second copy).
    private func outfitLoader(_ name: String) -> (String) -> CGImage? {
        return { [weak self] pose in
            guard let url = self?.spriteURL(pose, outfit: name),
                  let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
            return CGImageSourceCreateImageAtIndex(source, 0, [kCGImageSourceShouldCacheImmediately: true] as CFDictionary)
        }
    }

    /// 穿上应该穿的那套（启动、换日、菜单选择后都调这里）；已经是这套就什么都不做。
    func applyOutfit() {
        let name = chosenOutfit
        guard name != outfit else { return }
        outfit = name
        if petView != nil { petView.setOutfit(loader: outfitLoader(name)) }
        // 换了衣服，人在窗口里的宽度也变了：贴着屏幕边时重新对齐一次。
        if petWindow != nil, petWindow.isVisible {
            let origin = clampedPetOrigin(petWindow.frame.origin)
            if origin != petWindow.frame.origin {
                petWindow.setFrameOrigin(origin)
                defaults.set(NSStringFromPoint(origin), forKey: "petOrigin")
            }
        }
        outfitChanged()
        pushAvatar()
    }

    /// 任务面板左上角的头像跟着衣服换：每套都有自己的 pet-avatar（outfits/<套>/pet-avatar.webp）。
    private func pushAvatar() {
        guard pageReady, let url = spriteURL("avatar", outfit: outfit),
              let data = try? JSONSerialization.data(withJSONObject: [url.absoluteString]),
              let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.petSetAvatar && window.petSetAvatar(\(json)[0])", completionHandler: nil)
    }

    private func outfitMenuItem() -> NSMenuItem {
        let sub = NSMenu()
        let auto = NSMenuItem(title: "每天换一套", action: #selector(pickOutfit(_:)), keyEquivalent: "")
        auto.target = self
        auto.representedObject = ""
        sub.addItem(auto)
        outfitItems.append(auto)
        sub.addItem(.separator())
        for name in availableOutfits {
            let item = NSMenuItem(title: name, action: #selector(pickOutfit(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = name
            sub.addItem(item)
            outfitItems.append(item)
        }
        let parent = NSMenuItem(title: "衣服", action: nil, keyEquivalent: "")
        parent.submenu = sub
        outfitChanged()
        return parent
    }

    @objc func pickOutfit(_ sender: NSMenuItem) {
        defaults.set((sender.representedObject as? String) ?? "", forKey: "outfit")
        applyOutfit()
        outfitChanged()
    }

    /// 勾选状态：自动时勾「每天换一套」，今天这套显示为「–」；固定时勾那一套。
    private func outfitChanged() {
        let pinned = defaults.string(forKey: "outfit") ?? ""
        let isAuto = !availableOutfits.contains(pinned)
        for item in outfitItems {
            let value = (item.representedObject as? String) ?? ""
            if value.isEmpty { item.state = isAuto ? .on : .off }
            else { item.state = value == outfit ? (isAuto ? .mixed : .on) : .off }
        }
    }
    private var collectorHealthy = false
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
        statusMenu.addItem(withTitle: "退出二踢脚大王", action: #selector(quitWithGoodbye(_:)), keyEquivalent: "")
        statusMenu.items.forEach { $0.target = self }
        statusMenu.insertItem(sizeItem, at: 2)      // after 桌宠回到右下角; keeps its own targets
        statusMenu.insertItem(outfitMenuItem(), at: 3)
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

    /// 菜单和面板里的「退出」：先说再见（bye 停留 1.4 秒），再真正退出。
    ///
    /// 再见放在 terminate 之前，不用 applicationShouldTerminate 的 .terminateLater：那样 AppKit 会开一个
    /// 嵌套事件循环等回复，如果 terminate 是在主队列的一个任务里调用的（例如 Task 里），这个任务没结束，
    /// 主队列上的回复永远排不到，就卡死了。这里用 run loop 的 Timer，不经过 GCD 主队列。
    /// 其他方式的退出（系统注销、kill、直接调用 NSApp.terminate）照常立即退出。
    private var leaving = false
    @objc func quitWithGoodbye(_ sender: Any?) {
        if leaving { NSApp.terminate(nil); return }       // second click while saying bye: go now
        guard let petWindow = petWindow, petWindow.isVisible, let petView = petView else { NSApp.terminate(nil); return }
        leaving = true
        if panel?.isVisible == true { hidePanel() }        // don't hide her behind the panel
        petView.sayGoodbye()
        let t = Timer(timeInterval: PetMotion.farewell, repeats: false) { _ in NSApp.terminate(nil) }
        RunLoop.main.add(t, forMode: .common)
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
        outfit = chosenOutfit
        petView = PetView(loader: outfitLoader(outfit))
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
        menu.addItem(outfitMenuItem())
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "退出二踢脚大王", action: #selector(quitWithGoodbye(_:)), keyEquivalent: "")
        quit.target = self
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
        // 左右按看得见的人限制，不按整个透明窗口：窗口两边的透明区可以伸到屏幕外，人能贴到屏幕边。
        // 上下仍按整个窗口：头顶要留给气泡，脚下的影子本来就贴着窗口底边。
        let span: (min: CGFloat, max: CGFloat) = petView?.bodySpan ?? (min: 0, max: 1)
        let bodyLeft = size.width * span.min, bodyRight = size.width * span.max
        // 取整：窗口落在整点上，存下来的位置读回来不会差一点点又被挪一次。
        let minX = (f.minX - bodyLeft).rounded(.up), maxX = (f.maxX - bodyRight).rounded(.down)
        return NSPoint(x: min(max(origin.x, minX), max(minX, maxX)),
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
        applyOutfit()                                   // the day changed while on 每天换一套
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
            if item["userInterrupted"] as? Bool == true { return false }
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

    private func refresh() {
        let s = visibleState()
        let doneItems = (s["done"] as? [[String: Any]]) ?? []
        let running = (s["running"] as? [[String: Any]])?.count ?? 0
        // 明确按下停止的任务由采集器直接收起；旧版状态也不计入待处理。
        let needYou = ((s["pending"] as? [[String: Any]]) ?? [])
            .filter { ($0["state"] as? String) != "interrupted" && ($0["userInterrupted"] as? Bool) != true }
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
            pushAvatar()
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
            quitWithGoodbye(nil)
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
            try verify(idlePoses.isSuperset(of: ["idle", "blink", "edge-sit", "edge-sit-blink", "chin-rest", "guitar", "daze",
                                                 "sit", "sit-blink", "tea", "lie", "look", "stretch", "kneel", "legs-out",
                                                 "lie-side", "prone-read"]), "idle-variety")
            try verify(PetMotion.idleRoutine.allSatisfy { $0.1 >= 6 } && PetMotion.workRoutine.allSatisfy { $0.1 >= 7 }, "activities-held-long-enough")
            let seated = PetMotion.floor
            let seatedTime = PetMotion.idleRoutine.filter { seated.contains($0.0) }.reduce(0) { $0 + $1.1 }
            try verify(seatedTime >= idleTotal * 0.5, "not-always-standing")
            animation.running = true
            let workTotal = PetMotion.workRoutine.reduce(0) { $0 + $1.1 }
            let workingPoses = Set(stride(from: 0.0, to: workTotal, by: 0.1).map { animation.pose(at: $0) })
            try verify(workingPoses.isSuperset(of: ["type", "write", "write-blink", "read", "think",
                                                    "stance", "arms-crossed", "drumstick", "ending"]), "working-variety")
            animation.celebrationUntil = PetMotion.celebration
            try verify(animation.pose(at: 0.5) == "jump", "completion-jumps")
            try verify(animation.pose(at: 2) == "firecracker", "completion-firecracker")
            try verify(animation.pose(at: PetMotion.celebration - 0.5) == "happy", "completion-ends-with-a-smile")
            try verify(!PetMotion.cheers.contains(animation.pose(at: PetMotion.celebration + 1)), "unread-does-not-freeze-celebration")
            animation.greetingUntil = 9
            try verify(animation.pose(at: 8.1) == "wave" && animation.pose(at: 8.6) == "wave", "hover-waves")
            try verify(animation.pose(at: 8, reducedMotion: true) == "happy", "reduced-motion-greeting")
            // 挥手动画有节流，但不能长到让人觉得「悬停没反应」。
            try verify(PetMotion.greetingGap <= 3.5, "greeting-gap-is-short")
            try verify(animation.pose(at: 10, reducedMotion: true) == "type", "respects-reduced-motion")
            animation.dragging = true
            try verify(animation.pose(at: 10) == "drag", "drag-shows-carried-pose")
            animation.dragging = false
            animation.running = false
            animation.lastActivity = 0
            try verify(animation.pose(at: 400) == "sleep", "falls-asleep-when-quiet")
            animation.wakingUntil = 401
            try verify(animation.pose(at: 400.5) == "stretch", "wakes-up-stretching")
            animation.running = true
            try verify(animation.pose(at: 500) != "sleep", "work-keeps-her-awake")
            animation.leavingUntil = 600
            try verify(animation.pose(at: 599) == "bye" && animation.pose(at: 599, reducedMotion: true) == "bye", "quit-says-bye")
            animation.leavingUntil = 0
            try verify(PetMotion.allPoses.count == 33, "all-33-sprites-scheduled")
            for name in PetMotion.allPoses.sorted() {
                try verify(spriteURL(name, outfit: Outfits.fallback) != nil, "sprite-\(name)-bundled")
            }
            // 衣服：发布包带九套，仓库克隆只带一套；无论几套，每套的图都必须在位。
            // 只有本机（有 outfits/ 目录）才要求九套齐全。
            // run_native.py 打包时如果找到了九套，会设 PET_EXPECT_NINE_OUTFITS=1。
            let expectAllNine = ProcessInfo.processInfo.environment["PET_EXPECT_NINE_OUTFITS"] == "1"
            try verify(!availableOutfits.isEmpty, "at-least-one-outfit-bundled")
            if expectAllNine {
                try verify(availableOutfits == Outfits.order, "all-nine-outfits-bundled")
            }
            for name in availableOutfits {
                for pose in PetMotion.allPoses {
                    let url = spriteURL(pose, outfit: name)
                    try verify(url?.path.contains("/outfits/\(name)/") == true, "outfit-\(name)-\(pose)")
                }
                try verify(outfitLoader(name)("idle")?.width == 750, "outfit-\(name)-decodes")
            }
            // 轮换和换装只在有多套时才有意义；仓库克隆只带一套，跳过这些。
            if expectAllNine {
                let n = availableOutfits.count
                try verify((0..<30).allSatisfy { Set(Outfits.order(forRound: $0, from: availableOutfits)).count == n }, "each-round-nine-different")
                try verify((0..<30).allSatisfy { Outfits.order(forRound: $0, from: availableOutfits).last != Outfits.order(forRound: $0 + 1, from: availableOutfits).first }, "no-repeat-across-rounds")
                try verify(Set((0..<6).map { Outfits.order(forRound: $0, from: availableOutfits) }).count > 1, "rounds-are-shuffled")
                let savedOutfit = defaults.string(forKey: "outfit")
                defaults.set("月下青竹", forKey: "outfit"); applyOutfit()
                let before = petView.spriteShown
                defaults.set("琥珀节拍", forKey: "outfit"); applyOutfit()
                try verify(outfit == "琥珀节拍" && petView.spriteShown != nil && petView.spriteShown !== before, "outfit-switch-changes-sprite")
                let avatarSrc = try await js("decodeURIComponent(document.querySelector('.head img').getAttribute('src') || '')") as? String
                try verify(avatarSrc?.contains("/outfits/琥珀节拍/pet-avatar.") == true, "panel-avatar-follows-outfit")
                defaults.set("不存在的一套", forKey: "outfit"); applyOutfit()
                try verify(outfit == Outfits.daily(from: availableOutfits), "unknown-outfit-falls-back-to-daily")
                if let savedOutfit { defaults.set(savedOutfit, forKey: "outfit") } else { defaults.removeObject(forKey: "outfit") }
                applyOutfit()
            }
            // 内存：每次只解码少数几张，换遍全部姿势后缓存也不超过上限
            let loadAll = outfitLoader(outfit)
            petView.setOutfit(loader: loadAll)
            try verify(petView.spritesDecoded <= PetView.spriteCacheLimit, "sprite-cache-bounded")
            applyOutfit()
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
            // PET-LOCAL-FIXTURE-BEGIN 本机版用真实机器名（17/127），发布版用花名（Studio/Build）。
            rawState = [
                "agents": ["chatgpt": ["status": "ok", "open": true], "claude": ["status": "ok", "open": true], "deepseek": ["status": "ok", "open": true]],
                "hosts": [["id": "h1", "label": "Studio", "alias": "gpu-box", "machine": "gpu-box",
                           "status": "ok", "fetchedAt": now - 5, "claudeLive": 3],
                          ["id": "h2", "label": "Build", "alias": "cpu-box", "status": "offline",
                           "hint": "SSH 连接失败", "fetchedAt": now - 900]],
                "running": [["id": "test-run", "agent": "deepseek", "title": "正在整理项目资料", "prompt": "把本周实验结果汇总，列出接下来需要验证的内容", "start": now-180],
                           ["id": "test-remote", "agent": "chatgpt", "host": "h1", "title": "重构数据管线",
                            "prompt": "把读取与清洗拆成两个模块", "start": now-600, "open": ["type": "terminal", "value": "gpu-box"]]],
                "done": [["id": "test-done", "agent": "claude", "title": "本周总结已完成", "prompt": "请把实验记录整理成一份简短周报 <img src=x onerror=alert(1)>", "followups": ["顺便看下日志 <img src=x onerror=alert(2)>"], "followupCount": 3, "detail": "点击任务可打开对应应用", "time": now-60, "state": "success", "open": ["type": "app", "value": "invalid.test.app"]]],
                "pending": [["id": "test-pending", "agent": "chatgpt", "host": "h2", "title": "已主动中断的任务", "prompt": "帮我检查图标和动作", "detail": "已中断", "time": now-80, "state": "interrupted", "userInterrupted": true]]
            ]
            // PET-LOCAL-FIXTURE-END
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
            // Machine row: ALL (every machine, the default) first, then Mac and each server.
            let chips = try await js("Array.from(document.querySelectorAll('.mchip span')).map(e => e.textContent)") as? [String]
            // PET-LOCAL-FIXTURE-BEGIN
            try verify(chips == ["ALL", "Local", "Studio", "Build"], "machine-row-with-all")
            // PET-LOCAL-FIXTURE-END
            let dots = try await js("Array.from(document.querySelectorAll('.mchip .dot')).map(e => e.className)") as? [String]
            try verify(dots == ["dot done running", "dot done running", "dot none running", "dot none"], "machine-dot-worst-state")
            let offlineTag = try await js("document.querySelector('.mchip.offline em')?.textContent") as? String
            try verify(offlineTag == "离线", "offline-machine-marked")
            // Hover text gives the real numbers; a machine with a running task never says "没有任务".
            let tips = try await js("Array.from(document.querySelectorAll('.mchip')).map(e => e.title)") as? [String] ?? []
            try verify(tips.count == 4 && tips[2].hasSuffix("运行中 1") &&
                       tips.prefix(3).allSatisfy({ !$0.contains("没有任务") }) &&
                       tips[3].contains("没有任务"), "machine-tooltip-counts")
            let allRows = try await js("document.querySelectorAll('#list .row').length") as? Int
            try verify(allRows == 3, "all-machines-shows-everything")
            let badges = try await js("Array.from(document.querySelectorAll('#list .host-badge')).map(e => e.textContent).sort().join(',')") as? String
            // PET-LOCAL-FIXTURE-BEGIN
            try verify(badges == "Local,Local,Studio", "all-machines-rows-name-their-machine")
            // PET-LOCAL-FIXTURE-END
            _ = try await js("document.querySelector('[data-machine=\"__local__\"]').click()")
            try await Task.sleep(nanoseconds: 300_000_000)
            let macRows = try await js("document.querySelectorAll('#list .row').length") as? Int
            try verify(macRows == 2, "mac-shows-only-its-tasks")
            let allCount = try await js("document.getElementById('cntAll').textContent.trim()") as? String
            try verify(allCount == "2", "all-count-per-machine")
            let promptEscaped = try await js("document.querySelector('[data-view=\\\"test-done\\\"] .row-prompt')?.textContent.includes('<img src=x onerror=alert(1)>') == true && document.querySelector('[data-view=\\\"test-done\\\"] .row-prompt img') == null") as? Bool
            try verify(promptEscaped == true, "prompt-previews-escaped-and-visible")
            let followups = try await js("Array.from(document.querySelectorAll('[data-view=\\\"test-done\\\"] .followup')).map(e => e.textContent)") as? [String]
            try verify(followups == ["追加顺便看下日志 <img src=x onerror=alert(2)>"], "followups-visible-and-escaped")
            let followupNote = try await js("document.querySelector('[data-view=\\\"test-done\\\"] .followup-more')?.textContent") as? String
            try verify(followupNote == "更早还有 2 条追加", "earlier-followups-counted")
            // 角标 = 待查看的完成 + 需要你处理的；主动中断已经被收起。
            let badgeDone = (visibleState()["done"] as? [[String: Any]])?.count ?? 0
            let badgeNeed = ((visibleState()["pending"] as? [[String: Any]]) ?? [])
                .filter { ($0["state"] as? String) != "interrupted" }.count
            // 这份 fixture 里唯一的待处理是主动中断，已经不进入角标。
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
            // 菜单栏里不再有「服务器」（连接状态在任务面板的机器标签里看）；「衣服」列出九套。
            let topTitles = (statusItem?.menu?.items ?? []).map { $0.title }
            try verify(!topTitles.contains("服务器"), "no-server-menu")
            let outfitTitles = (statusItem?.menu?.items.first { $0.title == "衣服" }?.submenu?.items ?? []).map { $0.title }
            // 菜单里列出当前打包进来的每一套；九套齐全时再要求覆盖全部。
            try verify(availableOutfits.allSatisfy { outfitTitles.contains($0) }, "outfit-menu-lists-bundled")
            if ProcessInfo.processInfo.environment["PET_EXPECT_NINE_OUTFITS"] == "1" {
                try verify(Outfits.order.allSatisfy { outfitTitles.contains($0) }, "outfit-menu-lists-all-nine")
            }
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
            try verify(stoppedTag == nil, "user-interrupted-is-hidden")
            let attentionCount = try await js("document.getElementById('cntAttention').textContent.trim()") as? String
            try verify(attentionCount == "", "user-interrupted-not-needs-you")
            let doneCount = try await js("document.getElementById('cntDone').textContent.trim()") as? String
            try verify(doneCount == "", "user-interrupted-not-listed-under-done")
            logoSources.formUnion((try await js("Array.from(document.querySelectorAll('.logo img')).filter(i => i.complete && i.naturalWidth > 0).map(i => i.getAttribute('src'))") as? [String]) ?? [])
            try verify(logoSources.count == 3, "all-official-logos-loaded-offline")
            _ = try await js("document.querySelector('[data-machine=\"__all__\"]').click()")
            try await Task.sleep(nanoseconds: 300_000_000)
            let menuCount = statusItem?.menu?.items.count ?? 0
            try verify(menuCount == 6, "recovery-menu")
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
            try verify((visibleState()["done"] as? [[String: Any]])?.isEmpty == true, "refresh-no-resurrection")
            _ = try await js("document.querySelector('[data-act=close]').click()")
            try await Task.sleep(nanoseconds: 100_000_000)
            try verify(!panel.isVisible && petWindow.isVisible, "close-keeps-pet")
            petClicked()
            try verify(panel.isVisible, "pet-reopens-panel")
            // 位置按看得见的人限制：人在屏幕里，两边的透明区可以伸出去，人能贴到屏幕左右边。
            func bodyRect() -> NSRect {
                let f = petWindow.frame, s = petView.bodySpan
                return NSRect(x: f.minX + f.width * s.min, y: f.minY, width: f.width * (s.max - s.min), height: f.height)
            }
            try verify(petView.bodySpan.min > 0.05 && petView.bodySpan.max < 0.95 && petView.bodySpan.min < petView.bodySpan.max,
                       "body-span-measured-from-idle")
            petWindow.setFrameOrigin(NSPoint(x: -10000, y: -10000))
            petDragEnded()
            try verify(NSScreen.screens.contains { $0.visibleFrame.contains(bodyRect()) }, "drag-clamped-to-visible-screen")
            let edgeScreen = (petWindow.screen ?? NSScreen.main)!.visibleFrame
            try verify(abs(bodyRect().minX - (edgeScreen.minX + 4)) <= 1 && petWindow.frame.minX < edgeScreen.minX,
                       "drag-reaches-left-edge")
            petWindow.setFrameOrigin(NSPoint(x: edgeScreen.maxX + 10000, y: edgeScreen.minY + 40))
            petDragEnded()
            let rightScreen = (petWindow.screen ?? NSScreen.main)!.visibleFrame
            try verify(abs(bodyRect().maxX - (rightScreen.maxX - 4)) <= 1 && petWindow.frame.maxX > rightScreen.maxX,
                       "drag-reaches-right-edge")
            try verify(savedPetOrigin() == petWindow.frame.origin, "saved-drag-position")
            _ = applicationShouldHandleReopen(NSApp, hasVisibleWindows: true)
            try verify(savedPetOrigin() == petWindow.frame.origin, "reopen-preserves-location")
            // 退出走菜单的真实路径，而且就在这个 Task 里调用：以前这样会卡死（见 quitWithGoodbye）。
            // 卡住时进程不会退出，run_native.py 30 秒超时就会报失败。
            if !petWindow.isVisible { petWindow.orderFrontRegardless() }
            quitWithGoodbye(nil)
            try verify(petView.posePresented == "bye", "quit-shows-bye-before-exit")
            let report = "\(nativeCheckCount) native checks passed"
            try report.write(to: Paths.support.appendingPathComponent("native-result.txt"), atomically: true, encoding: .utf8)
            print(report)
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
