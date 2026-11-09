// Outreach notifier — posts one native macOS banner (like Messages) and exits.
//
//   open -n -a Outreach.app --args --title T --body B [--url U] [--reply R]
//
// Clicking the banner opens --url (the Gmail thread). The "Copy suggested reply" action copies
// --reply to the clipboard. macOS re-launches this app with no arguments when you click a
// banner after it has exited, which is why a no-argument launch just waits for the click.
import Cocoa
import UserNotifications

func argValue(_ name: String) -> String? {
    let a = CommandLine.arguments
    guard let i = a.firstIndex(of: name), i + 1 < a.count else { return nil }
    return a[i + 1]
}

func log(_ s: String) {
    // Errors go to a log file because nobody sees this app's stderr.
    let path = NSString(string: "~/.outreach-agent/notifier.log").expandingTildeInPath
    let line = "\(Date()) \(s)\n"
    if let h = FileHandle(forWritingAtPath: path) {
        h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); h.closeFile()
    } else {
        try? line.write(toFile: path, atomically: true, encoding: .utf8)
    }
}

final class Delegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let center = UNUserNotificationCenter.current()

    func applicationDidFinishLaunching(_ n: Notification) {
        center.delegate = self
        let copy = UNNotificationAction(identifier: "copy", title: "Copy suggested reply", options: [])
        center.setNotificationCategories([
            UNNotificationCategory(identifier: "reply", actions: [copy], intentIdentifiers: [], options: [])
        ])
        if let title = argValue("--title") {
            post(title: title, body: argValue("--body") ?? "")
        } else {
            // Launched by a banner click: the click arrives via didReceive. Don't hang around.
            DispatchQueue.main.asyncAfter(deadline: .now() + 8) { NSApp.terminate(nil) }
        }
    }

    func post(title: String, body: String) {
        center.requestAuthorization(options: [.alert, .sound]) { granted, err in
            if !granted {
                log("notifications not allowed (\(err?.localizedDescription ?? "denied")). "
                    + "Enable Outreach in System Settings > Notifications.")
                exit(2)
            }
            let c = UNMutableNotificationContent()
            c.title = title
            c.body = body
            c.sound = UNNotificationSound.default
            c.categoryIdentifier = "reply"
            var info: [String: String] = [:]
            if let u = argValue("--url") { info["url"] = u }
            if let r = argValue("--reply") { info["reply"] = r }
            c.userInfo = info
            self.center.add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil)) { e in
                if let e = e { log("could not post: \(e.localizedDescription)") }
                DispatchQueue.main.asyncAfter(deadline: .now() + 1) { NSApp.terminate(nil) }
            }
        }
    }

    // Show the banner even though this app is (briefly) the active one.
    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler h: @escaping (UNNotificationPresentationOptions) -> Void) {
        h([.banner, .sound])
    }

    func userNotificationCenter(_ c: UNUserNotificationCenter, didReceive r: UNNotificationResponse,
                                withCompletionHandler h: @escaping () -> Void) {
        let info = r.notification.request.content.userInfo
        if r.actionIdentifier == "copy", let text = info["reply"] as? String {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
        } else if let u = info["url"] as? String, let url = URL(string: u) {
            NSWorkspace.shared.open(url)
        }
        h()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { NSApp.terminate(nil) }
    }
}

let app = NSApplication.shared
let delegate = Delegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)     // no Dock icon
app.run()
