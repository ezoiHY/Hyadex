import AppKit
import HyadexCore

/// Development aid: an end-to-end pass through the app against a real server.
///
///     HYADEX_SERVER_URL=http://127.0.0.1:7872 HYADEX_TOKEN=… HYADEX_E2E_DIR=/tmp/e2e Hyadex
///
/// It drives the same `AppModel` methods the buttons call (send, approve,
/// group message), waits for the agents' real replies, writes a screenshot
/// per step and a log, then quits. Agents run for real and use tokens.
@MainActor
enum LiveCheck {
    static func runIfRequested(model: AppModel) async {
        guard let directory = ProcessInfo.processInfo.environment["HYADEX_E2E_DIR"] else { return }
        setvbuf(stdout, nil, _IONBF, 0)
        let output = URL(fileURLWithPath: directory, isDirectory: true)
        try? FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        let activity = ProcessInfo.processInfo.beginActivity(options: .userInitiated, reason: "Live check")
        defer { ProcessInfo.processInfo.endActivity(activity) }
        for window in NSApp.windows where window.canBecomeMain { window.makeKeyAndOrderFront(nil) }
        var failures = 0
        func log(_ text: String) { print("[e2e] " + text) }
        func check(_ ok: Bool, _ label: String) {
            log((ok ? "PASS " : "FAIL ") + label)
            if !ok { failures += 1 }
        }
        func shot(_ name: String) async {
            try? await Task.sleep(for: .seconds(1))
            Snapshotter.captureMain(to: output.appending(path: name + ".png"))
        }
        func wait(_ seconds: Double, _ condition: () -> Bool) async -> Bool {
            let end = Date.now.addingTimeInterval(seconds)
            while Date.now < end {
                if condition() { return true }
                try? await Task.sleep(for: .milliseconds(500))
            }
            return condition()
        }
        func pendingApproval(_ id: String) -> String? { model.timeline(id).pendingApproval?.id }
        func lastIsAgentReply(_ id: String) -> Bool {
            model.timeline(id).contains { item in
                if case .message(_, let author, let text, _, _, _, _) = item, author.kind == .agent, text.contains("e2e") { return true }
                return false
            }
        }

        check(await wait(20) { model.isLive && !model.chats.isEmpty }, "connected and listed \(model.chats.count) chats, \(model.groups.count) groups")
        model.showInspector = true
        let conversations = ProcessInfo.processInfo.environment["HYADEX_E2E_AUTOPILOT"] != "only"

        for backend in [AgentBackend.claude, .codex] where conversations {
            guard let chat = model.chats.first(where: { $0.backend == backend && $0.title.contains("mac e2e") }) else {
                check(false, "\(backend.rawValue) test chat exists"); continue
            }
            model.select(.chat(chat.id))
            check(await wait(15) { !model.timeline(chat.id).isEmpty }, "\(backend.rawValue): history loaded (\(model.timeline(chat.id).count) items)")
            await shot("\(backend.rawValue)-1-history")
            let marker = "e2e-\(backend.rawValue)-\(Int(Date.now.timeIntervalSince1970) % 100000)"
            let prompt = backend == .claude
                ? "Create \(marker).txt containing the word ok. Then reply in one sentence that mentions \(marker)."
                : "Run `echo \(marker)` in the shell and reply in one sentence that mentions \(marker)."
            let before = model.timeline(chat.id).count
            model.send(prompt, toChat: chat.id)
            check(await wait(30) { model.timeline(chat.id).count > before }, "\(backend.rawValue): sent turn appears in the timeline")
            check(await wait(30) { model.chat(chat.id)?.state == .running || model.chat(chat.id)?.state == .needsAction },
                  "\(backend.rawValue): chat shows running")
            if await wait(120, { pendingApproval(chat.id) != nil }), let approval = pendingApproval(chat.id) {
                check(model.chat(chat.id)?.state == .needsAction, "\(backend.rawValue): chat shows needs action")
                await shot("\(backend.rawValue)-2-approval")
                model.decide(approval, .allowed, in: chat.id)
                log("\(backend.rawValue): approved \(approval)")
            } else {
                log("\(backend.rawValue): no approval was requested")
            }
            check(await wait(180) { lastIsAgentReply(chat.id) && model.chat(chat.id)?.state == .idle },
                  "\(backend.rawValue): reply arrived and chat is idle")
            await shot("\(backend.rawValue)-3-done")
        }

        if !conversations {
            log("skipping chats and groups")
        } else if let group = model.groups.first(where: { $0.title.contains("mac e2e") }) {
            model.select(.group(group.id))
            _ = await wait(10) { !model.timeline(group.id).isEmpty }
            let before = model.timeline(group.id).count
            model.send("@codex reply with one short sentence that contains the word e2e-group. Do not edit files.",
                       toGroup: group.id, mode: .sequential)
            check(await wait(30) { model.timeline(group.id).count > before }, "group: message posted")
            check(await wait(180) {
                model.timeline(group.id).contains { item in
                    if case .message(_, let author, let text, _, _, _, _) = item, author.kind == .agent, text.contains("e2e-group") { return true }
                    return false
                }
            }, "group: @codex replied")
            _ = await wait(30) { model.group(group.id)?.state == .idle }
            await shot("group-1-reply")
        } else {
            check(false, "test group exists")
        }

        if ProcessInfo.processInfo.environment["HYADEX_E2E_AUTOPILOT"] != nil {
            if let job = model.jobs.first(where: { $0.title.contains("mac e2e") }) {
                model.select(.jobs)
                let wasEnabled = job.enabled
                model.setJob(job.id, enabled: !wasEnabled)
                check(await wait(15) { model.jobs.first { $0.id == job.id }?.enabled == !wasEnabled }, "jobs: toggled on the server")
                await shot("jobs-1-list")
                model.setJob(job.id, enabled: wasEnabled)
                _ = await wait(15) { model.jobs.first { $0.id == job.id }?.enabled == wasEnabled }
            } else {
                check(false, "jobs: test job listed (\(model.jobs.count) jobs)")
            }

            let marker = "e2e-run-\(Int(Date.now.timeIntervalSince1970) % 100000)"
            let known = Set(model.runs.map(\.id))
            model.createRun(goal: "Create a file named \(marker).md containing one line: hello from autopilot. Nothing else.",
                            cwd: "/tmp/hyadex-mac-e2e", approval: .plan, isolation: .worktree, reviewer: .planner,
                            verifyCommand: "test -s \(marker).md")
            check(await wait(30) { model.runs.contains { !known.contains($0.id) } }, "autopilot: run created")
            guard let run = model.runs.first(where: { !known.contains($0.id) }) else { exit(1) }
            check(await wait(300) { model.run(run.id)?.status == .awaitingApproval }, "autopilot: plan awaits approval")
            check(!(model.run(run.id)?.tasks.isEmpty ?? true), "autopilot: plan has \(model.run(run.id)?.tasks.count ?? 0) task(s)")
            await shot("autopilot-1-plan")
            model.approve(run: run.id)
            check(await wait(480) { [.done, .needsAttention, .aborted].contains(model.run(run.id)?.status) },
                  "autopilot: run finished as \(model.run(run.id)?.status.label ?? "?")")
            check(model.run(run.id)?.status == .done, "autopilot: done with \(model.run(run.id)?.completedTasks ?? 0) committed task(s)")
            await shot("autopilot-2-done")
        }

        log(failures == 0 ? "RESULT OK" : "RESULT \(failures) failure(s)")
        exit(failures == 0 ? 0 : 1)
    }
}
