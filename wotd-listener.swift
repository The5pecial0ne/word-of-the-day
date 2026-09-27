// wotd-listener: sits quietly in the background and asks wotd.py for a
// popup whenever you come back to your Mac.
//
// macOS announces the moment the screen wakes up or gets unlocked, so we
// just listen for that. No timers, no guessing. It uses almost no CPU while
// waiting, because it's asleep until macOS pokes it.
//
// Started by launchd as:  wotd-listener <path to python3> <path to wotd.py>

import AppKit
import CoreGraphics

let args = CommandLine.arguments
guard args.count >= 3 else {
    FileHandle.standardError.write("usage: wotd-listener <python3> <wotd.py>\n".data(using: .utf8)!)
    exit(1)
}
let python = args[1]
let script = args[2]
var popup: Process?

func screenIsLocked() -> Bool {
    guard let session = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
    return (session["CGSSessionScreenIsLocked"] as? Bool) ?? false
}

func showWord() {
    // One popup at a time. Waking and unlocking often happen a few seconds
    // apart, and two identical popups would just be annoying.
    if let current = popup, current.isRunning { return }

    let process = Process()
    process.executableURL = URL(fileURLWithPath: python)
    process.arguments = [script, "show"]
    do {
        try process.run()
        popup = process
    } catch {
        FileHandle.standardError.write("Couldn't start wotd.py: \(error)\n".data(using: .utf8)!)
    }
}

// The screen woke up. If the lock screen is showing, hold off and let the
// unlock below handle it, otherwise the popup would hide behind the lock screen.
NSWorkspace.shared.notificationCenter.addObserver(
    forName: NSWorkspace.screensDidWakeNotification, object: nil, queue: .main
) { _ in
    // Give macOS a second to put the lock screen up before we check for it.
    DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
        if !screenIsLocked() { showWord() }
    }
}

// You just unlocked with your password or Touch ID.
DistributedNotificationCenter.default().addObserver(
    forName: NSNotification.Name("com.apple.screenIsUnlocked"), object: nil, queue: .main
) { _ in
    showWord()
}

showWord()        // once when you log in, too
RunLoop.main.run() // then wait for macOS to call us, forever
