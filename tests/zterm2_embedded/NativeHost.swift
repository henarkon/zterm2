import AppKit
import ZTermEmbedded

@MainActor
private final class HostDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var window: NSWindow?
    private var terminals = [ZTermEmbeddedController]()
    private var closedInstances = Set<UUID>()
    private var closingWindow = false
    private var quitting = false
    private var cycling = false
    private var cycleIndex = 0
    private var fixture: URL?
    private var container: NSViewController?
    private var split: NSStackView?
    private var nativeControl: NSTextField?
    private var uiTimer: Timer?
    private var lastUISnapshot: Data?
    private var qualificationTimer: Timer?
    private var qualificationPhase = "ready"
    private var qualificationDeadline = Date.distantFuture
    private var observedExitCodes = [String: Int]()
    private var lifecycleProbes = [UUID: ZTermEmbeddedController]()
    private var completedProbes = Set<String>()
    private var apiQualification: Bool { ProcessInfo.processInfo.arguments.contains("--api-qualification") }

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            let arguments = ProcessInfo.processInfo.arguments
            guard let suiteIndex = arguments.firstIndex(of: "-suite"), arguments.indices.contains(suiteIndex + 1),
                  arguments[suiteIndex + 1] == "zterm2",
                  let fixture = Bundle.main.url(forResource: "fixture", withExtension: nil) else {
                throw CocoaError(.fileNoSuchFile)
            }
            try ZTermEmbeddedController.prepareCPUPrototypeRuntime(suiteName: "org.henarkon.zterm2.embedded.zterm2.demo")
            self.fixture = fixture
            let first = try ZTermEmbeddedController(width: 530, height: 600)
            let second = try ZTermEmbeddedController(width: 530, height: 600)
            terminals = [first, second]
            let container = NSViewController()
            self.container = container
            container.view = NSView(frame: NSRect(x: 0, y: 0, width: 1100, height: 700))
            terminals.forEach { container.addChild($0) }
            let nativeInput = NSTextField(string: "")
            nativeControl = nativeInput
            nativeInput.placeholderString = String(localized: "EmbeddedDemo.NativeControl", defaultValue: "Native focus control",
                                                   comment: "Placeholder of a native text field used to check focus outside the terminal")
            nativeInput.setAccessibilityIdentifier("embedded-native-input")
            let firstButton = NSButton(title: String(localized: "EmbeddedDemo.FocusFirst", defaultValue: "Focus first terminal",
                                                     comment: "Button moving keyboard focus to the first embedded terminal"),
                                       target: self, action: #selector(focusFirst))
            let secondButton = NSButton(title: String(localized: "EmbeddedDemo.FocusSecond", defaultValue: "Focus second terminal",
                                                      comment: "Button moving keyboard focus to the second embedded terminal"),
                                        target: self, action: #selector(focusSecond))
            let cycleButton = NSButton(title: String(localized: "EmbeddedDemo.NewPair", defaultValue: "New terminal pair",
                                                     comment: "Close both terminal instances and create a new pair in the same host process"),
                                       target: self, action: #selector(cycleTerminals))
            let toolbar = NSStackView(views: [nativeInput, firstButton, secondButton, cycleButton])
            toolbar.orientation = .horizontal
            let split = NSStackView(views: [first.view, second.view])
            self.split = split
            split.orientation = .horizontal
            split.distribution = .fillEqually
            let layout = NSStackView(views: [toolbar, split])
            layout.orientation = .vertical
            layout.alignment = .leading
            layout.translatesAutoresizingMaskIntoConstraints = false
            container.view.addSubview(layout)
            NSLayoutConstraint.activate([
                layout.leadingAnchor.constraint(equalTo: container.view.leadingAnchor, constant: 8),
                layout.trailingAnchor.constraint(equalTo: container.view.trailingAnchor, constant: -8),
                layout.topAnchor.constraint(equalTo: container.view.topAnchor, constant: 8),
                layout.bottomAnchor.constraint(equalTo: container.view.bottomAnchor, constant: -8),
                toolbar.heightAnchor.constraint(equalToConstant: 32),
                toolbar.widthAnchor.constraint(equalTo: layout.widthAnchor),
                split.widthAnchor.constraint(equalTo: layout.widthAnchor)
            ])
            let window = NSWindow(contentRect: container.view.frame, styleMask: [.titled, .closable, .resizable, .miniaturizable],
                                  backing: .buffered, defer: false)
            window.title = String(localized: "EmbeddedDemo.Title", defaultValue: "zterm2 Embedded — Native Host",
                                  comment: "Title of the separate native host used to qualify the embedded terminal")
            window.contentViewController = container
            window.delegate = self
            window.isReleasedWhenClosed = false
            self.window = window
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            try launchPair(fixture: fixture)
            first.focusTerminal()
            if arguments.contains("--ui-qualification") {
                uiTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
                    Task { @MainActor [weak self] in self?.recordUISnapshot() }
                }
            }
            if apiQualification {
                try runLifecycleProbes(fixture: fixture)
                qualificationDeadline = Date(timeIntervalSinceNow: 30)
                qualificationTimer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
                    Task { @MainActor [weak self] in self?.advanceQualification() }
                }
            }
        } catch {
            emit(["event": "startupFailed", "error": String(describing: error)])
            NSApp.terminate(nil)
        }
    }

    private func launchPair(fixture: URL) throws {
        emit(["event": "cycle", "index": cycleIndex, "hostPID": ProcessInfo.processInfo.processIdentifier])
        for (index, terminal) in terminals.enumerated() {
                terminal.eventHandler = { [weak self] record in
                    guard let self, let identifier = record["instance"] as? String,
                          let instance = UUID(uuidString: identifier) else { return }
                    self.emit(record)
                    if record["event"] as? String == "processExited", let code = record["exitCode"] as? Int {
                        self.observedExitCodes[identifier] = code
                    }
                    if ["closeTimedOut", "terminationStatusUnavailable", "launchFailed"].contains(record["event"] as? String ?? ""),
                       self.apiQualification { self.failQualification("Unexpected lifecycle event") }
                    if record["event"] as? String == "closed" {
                        if let terminal = self.terminals.first(where: { $0.instanceID == instance }) {
                            terminal.view.removeFromSuperview()
                            terminal.removeFromParent()
                        }
                        self.closedInstances.insert(instance)
                        if self.closedInstances.count == self.terminals.count {
                            self.terminals.removeAll()
                            self.emitResourceSample()
                            if self.quitting { DispatchQueue.main.async { NSApp.terminate(nil) } }
                            else if self.closingWindow {
                                if self.apiQualification {
                                    self.emit(["event": "apiQualificationClosed", "cycles": self.cycleIndex + 1])
                                }
                                self.window?.close()
                            }
                            else if self.cycling {
                                self.cycling = false
                                do { try self.createNextPair() }
                                catch {
                                    self.emit(["event": "cycleFailed", "error": String(describing: error)])
                                    NSApp.terminate(nil)
                                }
                            }
                        }
                    }
                }
                try terminal.launchExecutable(fixture, arguments: [index == 0 ? "alpha" : "beta", index == 0 ? "0" : "75"],
                                          environment: ["TERM": "xterm-256color", "LANG": "en_US.UTF-8", "PATH": "/usr/bin:/bin"],
                                          workingDirectory: FileManager.default.temporaryDirectory)
        }
    }

    private func recordUISnapshot() {
        let states: [[String: Any]] = terminals.map { terminal in
            var state: [String: Any] = ["instance": terminal.instanceID.uuidString]
            state["text"] = try? terminal.copyText(maximumCharacters: 100_000)
            state["metrics"] = terminal.terminalMetrics
            if let responder = window?.firstResponder as? NSView {
                state["focused"] = responder.isDescendant(of: terminal.view)
            }
            return state
        }
        let record: [String: Any] = ["event": "uiSnapshot", "terminals": states,
                                     "nativeText": nativeControl?.stringValue ?? ""]
        guard let encoded = try? JSONSerialization.data(withJSONObject: record, options: [.sortedKeys]),
              encoded != lastUISnapshot else { return }
        lastUISnapshot = encoded
        emit(record)
    }

    private func createNextPair() throws {
        guard let fixture, let split, let container else { throw CocoaError(.fileNoSuchFile) }
        closedInstances.removeAll()
        cycleIndex += 1
        qualificationPhase = "ready"
        qualificationDeadline = Date(timeIntervalSinceNow: 30)
        observedExitCodes.removeAll()
        let first = try ZTermEmbeddedController(width: 530, height: 600)
        let second = try ZTermEmbeddedController(width: 530, height: 600)
        terminals = [first, second]
        for terminal in terminals {
            container.addChild(terminal)
            split.addArrangedSubview(terminal.view)
        }
        try launchPair(fixture: fixture)
        first.focusTerminal()
    }

    private func advanceQualification() {
        guard !quitting, !closingWindow, !cycling else { return }
        guard Date() < qualificationDeadline else { failQualification("Timeout in \(qualificationPhase)"); return }
        do {
            guard terminals.count == 2 else { return }
            let texts = try terminals.map { try $0.copyText(maximumCharacters: 100_000) }
            switch qualificationPhase {
            case "ready":
                guard texts.enumerated().allSatisfy({ index, text in
                    text.contains("SIZE \(index == 0 ? "alpha" : "beta") ")
                }) else { return }
                for (index, terminal) in terminals.enumerated() {
                    try expectAPIError(code: 3) { _ = try terminal.sendInput(Data(repeating: 65, count: 4097)) }
                    let payload = "API-\(index)-été"
                    _ = try terminal.sendInput(Data(payload.utf8))
                }
                qualificationPhase = "input"
            case "input":
                for (index, text) in texts.enumerated() {
                    let hex = Data("API-\(index)-été".utf8).map { String(format: "%02x", $0) }.joined()
                    let instance = index == 0 ? "alpha" : "beta"
                    let joined = text.components(separatedBy: .newlines).filter { $0.hasPrefix("INPUT \(instance) ") }
                        .map { $0.replacingOccurrences(of: "INPUT \(instance) ", with: "") }.joined()
                    guard joined.contains(hex) else { return }
                }
                emit(["event": "apiInputVerified", "cycle": cycleIndex])
                for width in [620, 1200, 740, 1080] { window?.setContentSize(NSSize(width: width, height: 620)) }
                terminals.forEach { $0.refreshLayout() }
                qualificationPhase = "resize"
            case "resize":
                for (index, terminal) in terminals.enumerated() {
                    guard let metrics = terminal.terminalMetrics,
                          let columns = metrics["columns"]?.intValue, let rows = metrics["rows"]?.intValue,
                          let width = metrics["pixelWidth"]?.intValue, let height = metrics["pixelHeight"]?.intValue,
                          columns > 0, rows > 0,
                          texts[index].contains("SIZE \(index == 0 ? "alpha" : "beta") \(columns) \(rows) \(width) \(height)") else { return }
                }
                emit(["event": "apiResizeVerified", "cycle": cycleIndex,
                      "metrics": terminals.compactMap { $0.terminalMetrics }])
                if cycleIndex.isMultiple(of: 2) {
                    for terminal in terminals { _ = try terminal.sendInput(Data([4])) }
                    qualificationPhase = "exit"
                } else {
                    finishQualificationCycle()
                }
            case "exit":
                guard observedExitCodes.count == 2 else { return }
                guard observedExitCodes[terminals[0].instanceID.uuidString] == 0,
                      observedExitCodes[terminals[1].instanceID.uuidString] == 75 else {
                    failQualification("Incorrect natural exit status")
                    return
                }
                emit(["event": "apiExitCodesVerified", "cycle": cycleIndex])
                finishQualificationCycle()
            default: break
            }
        } catch { failQualification(String(describing: error)) }
    }

    private func finishQualificationCycle() {
        if cycleIndex == 11 {
            guard completedProbes == Set(["created", "starting"]) else { return }
            qualificationTimer?.invalidate()
            qualificationTimer = nil
            emit(["event": "apiQualificationInputComplete", "cycles": 12,
                  "uiKeyboardQualified": false, "hostPID": ProcessInfo.processInfo.processIdentifier])
            window?.performClose(nil)
        } else { cycleTerminals() }
    }

    private func expectAPIError(code: Int, operation: () throws -> Void) throws {
        do { try operation() }
        catch {
            let error = error as NSError
            if error.domain == "org.henarkon.zterm2.embedded" && error.code == code { return }
            throw error
        }
        throw CocoaError(.validationMissingMandatoryProperty)
    }

    private func runLifecycleProbes(fixture: URL) throws {
        for phase in ["created", "starting"] {
            let terminal = try ZTermEmbeddedController(width: 530, height: 600)
            let identifier = terminal.instanceID
            lifecycleProbes[identifier] = terminal
            try expectAPIError(code: 4) { _ = try terminal.sendInput(Data([65])) }
            try expectAPIError(code: 3) { _ = try terminal.copyText(maximumCharacters: 0) }
            try expectAPIError(code: 3) {
                try terminal.launchExecutable(fixture.appendingPathComponent("missing"), arguments: [], environment: [:],
                                              workingDirectory: FileManager.default.temporaryDirectory)
            }
            var records = [[String: Any]]()
            terminal.eventHandler = { [weak self] record in
                guard let self else { return }
                records.append(record)
                guard record["event"] as? String == "closed" else {
                    if ["closeTimedOut", "terminationStatusUnavailable", "launchFailed"].contains(record["event"] as? String ?? "") {
                        self.failQualification("Lifecycle probe failed: \(phase)")
                    }
                    return
                }
                do {
                    guard let terminal = self.lifecycleProbes[identifier] else { throw CocoaError(.fileNoSuchFile) }
                    try self.expectAPIError(code: 4) { _ = try terminal.copyText(maximumCharacters: 10) }
                    terminal.requestStop()
                    self.emit(["event": "apiLifecycleProbeClosed", "phase": phase, "records": records])
                    self.completedProbes.insert(phase)
                    self.lifecycleProbes.removeValue(forKey: identifier)
                    if self.quitting && self.terminals.isEmpty && self.lifecycleProbes.isEmpty {
                        DispatchQueue.main.async { NSApp.terminate(nil) }
                    }
                } catch { self.failQualification("Closed instance accepted an invalid operation") }
            }
            if phase == "starting" {
                try terminal.launchExecutable(fixture, arguments: ["probe", "0"],
                                              environment: ["TERM": "xterm-256color", "PATH": "/usr/bin:/bin"],
                                              workingDirectory: FileManager.default.temporaryDirectory)
            }
            terminal.requestStop()
            terminal.requestStop()
        }
    }

    private func failQualification(_ reason: String) {
        qualificationTimer?.invalidate()
        qualificationTimer = nil
        emit(["event": "apiQualificationFailed", "phase": qualificationPhase, "reason": reason])
        NSApp.terminate(nil)
    }

    private func emitResourceSample() {
        var info = mach_task_basic_info()
        var count = mach_msg_type_number_t(MemoryLayout<mach_task_basic_info>.size / MemoryLayout<integer_t>.size)
        let status = withUnsafeMutablePointer(to: &info) { pointer in
            pointer.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                task_info(mach_task_self_, task_flavor_t(MACH_TASK_BASIC_INFO), $0, &count)
            }
        }
        var record: [String: Any] = ["event": "resourcesAfterPairClose", "cycle": cycleIndex,
                                    "hostPID": ProcessInfo.processInfo.processIdentifier]
        record["openDescriptors"] = try? FileManager.default.contentsOfDirectory(atPath: "/dev/fd").count
        if status == KERN_SUCCESS { record["residentBytes"] = info.resident_size }
        emit(record)
    }

    @objc private func cycleTerminals() {
        guard !quitting, !closingWindow, !cycling else { return }
        cycling = true
        terminals.forEach { $0.requestStop() }
    }

    @objc private func focusFirst() { terminals.first?.focusTerminal() }
    @objc private func focusSecond() { terminals.last?.focusTerminal() }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard !terminals.isEmpty else { return true }
        closingWindow = true
        terminals.forEach { $0.requestStop() }
        return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !terminals.isEmpty || !lifecycleProbes.isEmpty else {
            if apiQualification { emit(["event": "apiHostTerminating", "hostPID": ProcessInfo.processInfo.processIdentifier]) }
            return .terminateNow
        }
        quitting = true
        terminals.forEach { $0.requestStop() }
        lifecycleProbes.values.forEach { $0.requestStop() }
        return .terminateCancel
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func windowWillClose(_ notification: Notification) {
        DispatchQueue.main.async { [weak self] in
            guard let self, self.terminals.isEmpty, self.lifecycleProbes.isEmpty else { return }
            self.window = nil
            self.container = nil
            self.split = nil
            NSApp.terminate(nil)
        }
    }

    private func emit(_ record: [String: Any]) {
        if var data = try? JSONSerialization.data(withJSONObject: record, options: [.sortedKeys]) {
            data.append(10)
            FileHandle.standardOutput.write(data)
        }
    }
}

@main
private enum NativeHost {
    @MainActor
    static func main() {
        let application = NSApplication.shared
        application.setActivationPolicy(.regular)
        let delegate = HostDelegate()
        application.delegate = delegate
        let menu = NSMenu()
        let app = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: String(localized: "EmbeddedDemo.Quit", defaultValue: "Quit",
                                         comment: "Menu action quitting the embedded-terminal demo host"),
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        app.submenu = appMenu
        menu.addItem(app)
        let edit = NSMenuItem(title: String(localized: "EmbeddedDemo.Edit", defaultValue: "Edit",
                                           comment: "Edit menu in the embedded-terminal demo host"), action: nil, keyEquivalent: "")
        let editMenu = NSMenu()
        editMenu.addItem(withTitle: String(localized: "EmbeddedDemo.Copy", defaultValue: "Copy", comment: "Standard copy action"),
                         action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: String(localized: "EmbeddedDemo.Paste", defaultValue: "Paste", comment: "Standard paste action"),
                         action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.submenu = editMenu
        menu.addItem(edit)
        application.mainMenu = menu
        withExtendedLifetime(delegate) { application.run() }
    }
}
