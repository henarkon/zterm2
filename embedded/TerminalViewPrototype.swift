import AppKit

@MainActor
private final class EmbeddedApplicationContext: NSObject {
    private weak var application: NSApplication?
    @objc private(set) var it_windowBecomingKey: NSWindow?

    init(application: NSApplication) {
        self.application = application
    }

    @objc var delegate: NSObject? { nil }
    @objc var it_modifierFlags: NSEvent.ModifierFlags { NSEvent.modifierFlags }
    @objc var it_keyWindow: NSWindow? { application?.keyWindow?.attachedSheet ?? application?.keyWindow }
    @objc var it_modalWindowOpen: Bool { application?.modalWindow != nil }
    @objc var it_characterPanelIsOpen: Bool { false }
    @objc var it_imeOpen: Bool { false }
    @objc var it_accentMenuOpen: Bool { false }
    @objc var it_justBecameActive: Bool { false }
    @objc var localAuthenticationDialogOpen: Bool { false }
    @objc var isUIElement: Bool { application?.activationPolicy() != .regular }
    @objc var it_windowsWithSheetModals: [NSWindow] { application?.windows.filter { $0.attachedSheet != nil } ?? [] }

    @objc func it_functionPressed() -> Bool { NSEvent.modifierFlags.contains(.function) }
    @objc func orderedWindowsPlusVisibleHotkeyPanels() -> [NSWindow] { application?.orderedWindows ?? [] }
    @objc func orderedWindowsPlusAllHotkeyPanels() -> [NSWindow] { application?.orderedWindows ?? [] }
    @objc func orderedScriptingWindows() -> [NSObject] { [] }
    @objc func routeEventToShortcutInputView(_ event: NSEvent) -> Bool { false }

    @objc func it_makeWindowKey(_ window: NSWindow) {
        it_windowBecomingKey = window
        window.makeKey()
        it_windowBecomingKey = nil
    }

    @objc func activateApp(completion: @escaping @Sendable () -> Void) {
        application?.activate(ignoringOtherApps: true)
        DispatchQueue.main.async(execute: completion)
    }

    override func forwardingTarget(for selector: Selector!) -> Any? {
        if let application, application.responds(to: selector) { return application }
        return super.forwardingTarget(for: selector)
    }

    override func responds(to selector: Selector!) -> Bool {
        super.responds(to: selector) || application?.responds(to: selector) == true
    }
}

@MainActor
@objc(ZTermEmbeddedController)
public final class TerminalViewPrototype: NSViewController {
    public enum PrototypeError: Int, Error, CustomNSError {
        case runtimeNotPrepared = 1
        case incompatibleRuntime = 2
        case invalidConfiguration = 3
        case invalidState = 4
        case engineInitializationFailed = 5
        case wouldBlock = 6

        public static var errorDomain: String { "org.henarkon.zterm2.embedded" }
        public var errorCode: Int { rawValue }
    }

    public enum Event {
        case started
        case launchFailed(Int, Int?)
        case processExited(rawStatus: Int32, exitCode: Int?, signal: Int?, requested: Bool)
        case terminationStatusUnavailable
        case closeTimedOut
        case closed
    }

    public struct LaunchConfiguration {
        public let executable: URL
        public let arguments: [String]
        public let environment: [String: String]
        public let workingDirectory: URL

        public init(executable: URL, arguments: [String], environment: [String: String], workingDirectory: URL) {
            self.executable = executable
            self.arguments = arguments
            self.environment = environment
            self.workingDirectory = workingDirectory
        }
    }

    @objc public static let sdkVersion = "0.2.0"
    public static let qualifiesM1 = false
    public static let inheritsHostEnvironment = false
    @objc public let instanceID = UUID()
    public var onEvent: ((UUID, Event) -> Void)?
    @objc public var eventHandler: (([String: Any]) -> Void)?
    @objc public private(set) var eventSequence: UInt64 = 0

    private static var preparedSuite: String?
    private var session: PTYSession!
    private var exitObservation: NSObjectProtocol?
    private var launched = false
    private var stopping = false
    private var launchFinished = false
    private var processExpected = false
    private var processReaped = false
    private var ioDrained = false
    private var parserDrained = false
    private var engineTerminated = false
    private var shutdownStarted = false
    private var closed = false
    private var bufferedReap: (received: Bool, status: NSNumber?) = (false, nil)
    private var lastLayout: (columns: Int32, rows: Int32, bounds: NSSize, scale: CGFloat)?

    @objc public static func prepareCPUPrototypeRuntime(suiteName: String) throws {
        guard suiteName.hasPrefix("org.henarkon.zterm2.embedded."), suiteName.count < 128 else {
            throw PrototypeError.invalidConfiguration
        }
        if let preparedSuite {
            guard preparedSuite == suiteName else { throw PrototypeError.incompatibleRuntime }
            return
        }
        guard iTermUserDefaults.customSuiteName() == nil else {
            throw PrototypeError.incompatibleRuntime
        }
        guard iTermAPIHelper.disableForEmbeddedHost() else {
            throw PrototypeError.incompatibleRuntime
        }
        iTermResourceLimitsHelperSaveCurrentLimits()
        iTermApplication.registerEmbeddedApplicationContext(EmbeddedApplicationContext(application: NSApplication.shared))
        iTermUserDefaults.setCustomSuiteName(suiteName)
        iTermUserDefaults.userDefaults().register(defaults: [
            kPreferenceKeyUseMetal: false,
            "RunJobsInServers": false,
            "UseTaskIOMonitors": true
        ])
        iTermPreferences.initializeUserDefaults()
        PTYSession.registerBuiltInFunctions()
        PTYTab.registerBuiltInFunctions()
        iTermBuiltInFunctions.registerStandardFunctions()
        let defaults = iTermUserDefaults.userDefaults()
        guard !defaults.bool(forKey: "RunJobsInServers"), defaults.bool(forKey: "UseTaskIOMonitors"),
              !defaults.bool(forKey: kPreferenceKeyUseMetal), TaskNotifier.usesTaskIOMonitors(),
              !iTermAdvancedSettingsModel.runJobsInServers() else {
            throw PrototypeError.incompatibleRuntime
        }
        preparedSuite = suiteName
    }

    public init(size: NSSize) throws {
        guard Self.preparedSuite != nil else { throw PrototypeError.runtimeNotPrepared }
        guard size.width.isFinite, size.height.isFinite, size.width >= 100, size.height >= 50 else {
            throw PrototypeError.invalidConfiguration
        }
        guard let engine = PTYSession(synthetic: true) else {
            throw PrototypeError.engineInitializationFailed
        }
        session = engine
        session.embeddedHost = true
        super.init(nibName: nil, bundle: nil)
        var profile = iTermProfilePreferences.defaultValueMap().filter { !($0.value is NSNull) }
        profile[KEY_GUID] = instanceID.uuidString
        profile[KEY_NAME] = instanceID.uuidString
        profile[KEY_UNDO_TIMEOUT] = 0
        profile[KEY_XTERM_MOUSE_REPORTING] = true
        session.profile = profile
        session.setScreenSize(size, parent: nil)
        session.textview?.usesHostWindowFocus = true
        session.setPreferencesFromAddressBookEntry(profile)
        session.loadInitialColorTableAndResetCursorGuide()
        guard let terminalView = session.view else {
            throw PrototypeError.engineInitializationFailed
        }
        view = NSView(frame: NSRect(origin: .zero, size: size))
        terminalView.frame = view.bounds
        terminalView.autoresizingMask = [.width, .height]
        view.addSubview(terminalView)
        session.active = true
        exitObservation = NotificationCenter.default.addObserver(
            forName: .PTYSessionTerminated, object: session, queue: .main
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, !self.closed, self.shutdownStarted, self.session?.textview == nil else { return }
                self.engineTerminated = true
                self.finishShutdownIfReady()
            }
        }
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { nil }

    @objc public convenience init(width: CGFloat, height: CGFloat) throws {
        try self.init(size: NSSize(width: width, height: height))
    }

    @objc public func launchExecutable(_ executable: URL, arguments: [String], environment: [String: String],
                                      workingDirectory: URL) throws {
        try launch(.init(executable: executable, arguments: arguments, environment: environment, workingDirectory: workingDirectory))
    }

    @objc public func sendInput(_ data: Data) throws -> NSNumber {
        guard launched, launchFinished, processExpected, !processReaped, !stopping else { throw PrototypeError.invalidState }
        guard data.count <= 4096 else { throw PrototypeError.invalidConfiguration }
        guard session.shell.writeBufferHasRoom else { throw PrototypeError.wouldBlock }
        session.shell.write(data)
        return NSNumber(value: data.count)
    }

    @objc public func refreshLayout() {
        guard !stopping else { return }
        view.layoutSubtreeIfNeeded()
        resizeTerminal()
    }

    @objc public func copyText(maximumCharacters: Int) throws -> String {
        guard maximumCharacters > 0, maximumCharacters <= 1_048_576 else { throw PrototypeError.invalidConfiguration }
        guard !closed, let textView = session?.textview else { throw PrototypeError.invalidState }
        return String(textView.content.suffix(maximumCharacters))
    }

    @objc public var terminalMetrics: [String: NSNumber]? {
        guard !closed, let session else { return nil }
        let size = session.screen.viewSize
        let scale = view.window?.backingScaleFactor ?? 1
        return ["columns": NSNumber(value: session.screen.width()), "rows": NSNumber(value: session.screen.height()),
                "pixelWidth": NSNumber(value: Double(size.width * scale)), "pixelHeight": NSNumber(value: Double(size.height * scale)),
                "backingScale": NSNumber(value: Double(scale)), "usesMetal": NSNumber(value: session.useMetal)]
    }

    deinit {
        if let exitObservation {
            NotificationCenter.default.removeObserver(exitObservation)
        }
    }

    public func launch(_ configuration: LaunchConfiguration) throws {
        guard !launched, !stopping else { throw PrototypeError.invalidState }
        let directory = configuration.workingDirectory
        guard configuration.executable.isFileURL, directory.isFileURL,
              configuration.executable.path.hasPrefix("/"), directory.path.hasPrefix("/"),
              FileManager.default.isExecutableFile(atPath: configuration.executable.path),
              (try? directory.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true,
              !configuration.arguments.contains(where: { $0.utf8.contains(0) }),
              configuration.environment.allSatisfy({ key, value in
                  !key.isEmpty && !key.contains("=") && !key.utf8.contains(0) && !value.utf8.contains(0)
              }) else {
            throw PrototypeError.invalidConfiguration
        }
        launched = true
        resizeTerminal()
        var environment = configuration.environment
        environment["PWD"] = directory.path
        session.shell.launchWithExplicitEnvironment = true
        session.shell.processExitHandler = { [weak self] rawStatus in
            Task { @MainActor [weak self] in
                guard let self, !self.closed else { return }
                self.recordReap(rawStatus)
            }
        }
        session.shell.launchResultHandler = { [weak self] status, errorCode in
            Task { @MainActor [weak self] in
                guard let self, !self.closed else { return }
                self.launchFinished = true
                self.processExpected = status == .success
                if status == .success {
                    if !self.stopping { self.emit(.started) }
                } else {
                    self.emit(.launchFailed(Int(status.rawValue), errorCode?.intValue))
                }
                if self.bufferedReap.received {
                    let status = self.bufferedReap.status
                    self.bufferedReap = (false, nil)
                    self.recordReap(status)
                }
                if self.stopping { self.beginShutdown() }
            }
        }
        session.shell.launch(withPath: configuration.executable.path,
                             arguments: configuration.arguments,
                             environment: environment,
                             customShell: nil,
                             gridSize: VT100GridSizeMake(session.screen.width(), session.screen.height()),
                             viewSize: session.screen.viewSize,
                             maybeScaleFactor: view.window?.backingScaleFactor ?? 1,
                             isUTF8: true, completion: nil)
    }

    public override func viewDidLayout() {
        super.viewDidLayout()
        resizeTerminal()
    }

    @objc public func focusTerminal() {
        guard !stopping else { return }
        view.window?.makeFirstResponder(session.textview)
    }

    @objc public func requestStop() {
        guard !stopping else { return }
        stopping = true
        if !launched || launchFinished { beginShutdown() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
            guard let self, !self.closed else { return }
            self.emit(.closeTimedOut)
        }
    }

    private func beginShutdown() {
        guard !shutdownStarted else { return }
        shutdownStarted = true
        session.terminate()
        session.shell.deregister { [weak self] in
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.ioDrained = true
                zterm2_drain_screen(self.session.screen) { [weak self] in
                    Task { @MainActor [weak self] in
                        guard let self else { return }
                        self.parserDrained = true
                        self.finishShutdownIfReady()
                    }
                }
            }
        }
    }

    private func finishShutdownIfReady() {
        guard stopping, !closed, ioDrained, parserDrained, engineTerminated,
              (!launched || launchFinished), (!processExpected || processReaped) else { return }
        closed = true
        if let exitObservation {
            NotificationCenter.default.removeObserver(exitObservation)
            self.exitObservation = nil
        }
        view.subviews.forEach { $0.removeFromSuperview() }
        session.shell.processExitHandler = nil
        session.shell.launchResultHandler = nil
        session = nil
        emit(.closed)
        onEvent = nil
        eventHandler = nil
    }

    private func recordReap(_ rawStatus: NSNumber?) {
        if !launchFinished {
            bufferedReap = (true, rawStatus)
            return
        }
        guard let rawStatus else {
            emit(.terminationStatusUnavailable)
            return
        }
        processReaped = true
        let raw = rawStatus.int32Value
        let code = zterm2_exit_code(raw)
        let signal = zterm2_exit_signal(raw)
        emit(.processExited(rawStatus: raw, exitCode: code >= 0 ? Int(code) : nil,
                            signal: signal > 0 ? Int(signal) : nil, requested: stopping))
        finishShutdownIfReady()
    }

    private func emit(_ event: Event) {
        eventSequence += 1
        var record: [String: Any] = ["instance": instanceID.uuidString, "sequence": eventSequence, "version": Self.sdkVersion]
        switch event {
        case .started:
            record["event"] = "started"
            record["childPID"] = session.shell.pid
            record["metrics"] = terminalMetrics
        case let .launchFailed(code, systemError):
            record["event"] = "launchFailed"
            record["code"] = code
            record["systemError"] = systemError
        case let .processExited(raw, code, signal, requested):
            record["event"] = "processExited"
            record["rawStatus"] = raw
            record["exitCode"] = code
            record["signal"] = signal
            record["requested"] = requested
        case .terminationStatusUnavailable: record["event"] = "terminationStatusUnavailable"
        case .closeTimedOut: record["event"] = "closeTimedOut"
        case .closed: record["event"] = "closed"
        }
        eventHandler?(record)
        onEvent?(instanceID, event)
    }

    private func resizeTerminal() {
        guard !stopping, let textView = session.textview, let terminalView = session.view else { return }
        let available = terminalView.scrollview.contentSize
        guard available.width.isFinite, available.height.isFinite,
              available.width > 0, available.height > 0,
              textView.charWidth.isFinite, textView.lineHeight.isFinite,
              textView.charWidth > 0, textView.lineHeight > 0 else { return }
        let columns = Int32(max(1, min(4096, (available.width - CGFloat(iTermPreferences.sideMargins() * 2)) / textView.charWidth)))
        let rows = Int32(max(1, min(4096, (available.height - CGFloat(iTermPreferences.topBottomMargins() * 2)) / textView.lineHeight)))
        let scale = view.window?.backingScaleFactor ?? 1
        if let lastLayout, lastLayout.columns == columns, lastLayout.rows == rows,
           lastLayout.bounds == available, lastLayout.scale == scale { return }
        lastLayout = (columns, rows, available, scale)
        session.setSize(VT100GridSizeMake(columns, rows))
    }
}
