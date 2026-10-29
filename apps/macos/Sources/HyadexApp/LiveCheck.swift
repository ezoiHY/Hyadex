import AppKit
import HyadexCore
import HyadexUI
import UserNotifications

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
        // HYADEX_UI_PROBE=1: print the window's accessibility tree (demo data is enough) and quit.
        if ProcessInfo.processInfo.environment["HYADEX_UI_PROBE"] != nil {
            setvbuf(stdout, nil, _IONBF, 0)
            // In the background, App Nap would stretch every sleep.
            _ = ProcessInfo.processInfo.beginActivity(options: .userInitiated, reason: "UI probe")
            for window in NSApp.windows where window.canBecomeMain { window.orderFrontRegardless() }
            try? await Task.sleep(for: .seconds(2))
            if let chat = model.chats.first(where: { $0.title.contains("features e2e claude") }) ?? model.chats.first {
                model.select(.chat(chat.id))
            }
            try? await Task.sleep(for: .seconds(2))
            UIDriver.probeSystemAX()
            try? await Task.sleep(for: .seconds(1))
            UIDriver.dump()
            exit(0)
        }
        guard let directory = ProcessInfo.processInfo.environment["HYADEX_E2E_DIR"] else { return }
        setvbuf(stdout, nil, _IONBF, 0)
        let output = URL(fileURLWithPath: directory, isDirectory: true)
        try? FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        let activity = ProcessInfo.processInfo.beginActivity(options: .userInitiated, reason: "Live check")
        defer { ProcessInfo.processInfo.endActivity(activity) }
        // Placed off every screen and shown without taking keyboard focus: the
        // check never covers the user's work, and their keystrokes stay theirs.
        for window in NSApp.windows where window.styleMask.contains(.titled) && !(window is NSPanel) && window.frame.width > 600 {
            window.setFrameOrigin(NSPoint(x: -20000, y: 200))
            if window.isMiniaturized { window.deminiaturize(nil) }
            window.orderFrontRegardless()
        }
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
        check(await wait(60) { model.usage[.claude] != nil && model.usage[.codex] != nil }, "usage: both providers answered")
        for backend in AgentBackend.allCases {
            let usage = model.usage[backend]
            log("usage \(backend.rawValue): " + (usage?.available == true
                ? usage!.windows.map { "\($0.label) \(Int($0.usedPercent))%" }.joined(separator: ", ")
                : usage?.unavailableText ?? "-"))
        }
        // HYADEX_E2E_UI=only: the same features through the real controls (see UIDriver).
        if ProcessInfo.processInfo.environment["HYADEX_E2E_UI"] == "gaps" {
            UIDriver.enable()
            await uiGaps(model: model, check: check, shot: shot)
            log("RESULT " + (failures == 0 ? "OK" : "\(failures) FAILED"))
            exit(failures == 0 ? 0 : 1)
        }
        if ProcessInfo.processInfo.environment["HYADEX_E2E_UI"] != nil {
            await uiDrive(model: model, check: check, shot: shot)
            log("RESULT " + (failures == 0 ? "OK" : "\(failures) FAILED"))
            exit(failures == 0 ? 0 : 1)
        }
        // HYADEX_E2E_EXTRAS=1|only: unread, full-text search, fork, side chat and subagents.
        if let mode = ProcessInfo.processInfo.environment["HYADEX_E2E_EXTRAS"] {
            await extras(model: model, check: check, shot: shot)
            if mode == "only" {
                log("RESULT " + (failures == 0 ? "OK" : "\(failures) FAILED"))
                exit(failures == 0 ? 0 : 1)
            }
        }
        // HYADEX_E2E_MANAGE=1|only: attachments, rename/archive/delete, jobs, server folders,
        // queue order and Force Send, emergency alerts, older history and compaction.
        if let mode = ProcessInfo.processInfo.environment["HYADEX_E2E_MANAGE"] {
            await manage(model: model, check: check, shot: shot)
            if mode == "only" {
                log("RESULT " + (failures == 0 ? "OK" : "\(failures) FAILED"))
                exit(failures == 0 ? 0 : 1)
            }
        }
        // HYADEX_E2E_CAPABILITIES=1|only: native goals, slash commands and skills, MCP servers
        // and a group goal, on chats titled "features e2e claude" / "features e2e codex".
        if let mode = ProcessInfo.processInfo.environment["HYADEX_E2E_CAPABILITIES"] {
            await capabilities(model: model, check: check, shot: shot)
            if mode == "only" {
                log("RESULT " + (failures == 0 ? "OK" : "\(failures) FAILED"))
                exit(failures == 0 ? 0 : 1)
            }
        }
        // HYADEX_E2E_TERMINAL=1|only: drive the chat's tmux terminal through the real PTY socket.
        if let mode = ProcessInfo.processInfo.environment["HYADEX_E2E_TERMINAL"],
           let chat = model.chats.first(where: { $0.backend == .codex && $0.title.contains("mac e2e") }) {
            model.select(.chat(chat.id))
            model.terminalHeight = 260
            if !model.terminalOpen.contains(chat.id) { model.toggleTerminal(chat.id) }
            let session = model.terminalSession(for: chat.id)
            func screen() -> String {
                guard let emulator = session?.emulator else { return "" }
                return emulator.text(from: (0, 0), to: (emulator.columns - 1, emulator.rows - 1))
            }
            check(await wait(20) { session?.phase == .connected }, "terminal: attached to the chat's tmux session")
            _ = await wait(3) { !screen().trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
            let marker = "hxterm\(Int(Date.now.timeIntervalSince1970) % 100000)"
            session?.send("echo \(marker)-$((6*7))\r")
            check(await wait(10) { screen().contains("\(marker)-42") }, "terminal: typed command ran and its output rendered")
            session?.send("printf '\\033[31m%s\\033[0m\\n' red-\(marker); echo 中文-\(marker)\r")
            let colored = await wait(10) {
                guard let emulator = session?.emulator else { return false }
                return emulator.lines.contains { row in row.contains { $0.text == "r" && $0.style.foreground == .indexed(1) } }
            }
            check(colored, "terminal: SGR colour reaches the grid")
            check(await wait(10) { screen().contains("中文-\(marker)") }, "terminal: wide (CJK) characters render")
            let rowsBefore = session?.emulator.rows ?? 0
            model.terminalHeight = 420
            check(await wait(5) { (session?.emulator.rows ?? 0) > rowsBefore }, "terminal: panel resize changes the grid (\(rowsBefore) → \(session?.emulator.rows ?? 0) rows)")
            // A full-screen program: vim draws through tmux, then the shell returns.
            session?.send("vim -u NONE -N -c 'call setline(1, \"vim-\(marker)\")'\r")
            check(await wait(10) { screen().contains("vim-\(marker)") && screen().contains("~") }, "terminal: vim draws a full screen")
            session?.send("\u{1B}:q!\r")
            check(await wait(10) { !screen().contains("vim-\(marker)") || screen().contains("\(marker)-42") }, "terminal: quitting vim restores the shell")
            await model.refreshTerminalWindows(chat.id)
            let windowsBefore = model.terminalWindows[chat.id]?.count ?? 0
            check(windowsBefore >= 1, "terminal: tmux windows listed (\(windowsBefore))")
            model.terminalAction(chat.id, "new-window")
            check(await wait(8) { (model.terminalWindows[chat.id]?.count ?? 0) == windowsBefore + 1 }, "terminal: new tmux window")
            if let added = model.terminalWindows[chat.id]?.last {
                model.terminalAction(chat.id, "kill-window", target: String(added.index))
                check(await wait(8) { (model.terminalWindows[chat.id]?.count ?? 0) == windowsBefore }, "terminal: closed the new window")
            }
            await shot("terminal")
            model.toggleTerminal(chat.id)
            check(await wait(5) { session?.phase != .connected }, "terminal: hiding the panel detaches")
            model.toggleTerminal(chat.id)
            check(await wait(20) { session?.phase == .connected && screen().contains("\(marker)-42") },
                  "terminal: reattaching repaints the still-running session")
            if mode == "only" {
                log("RESULT " + (failures == 0 ? "OK" : "\(failures) FAILED"))
                NSApp.terminate(nil)
                return
            }
        }
        // HYADEX_E2E_RICH=claude|codex: ask for a Markdown and math answer and capture it.
        if let rich = ProcessInfo.processInfo.environment["HYADEX_E2E_RICH"],
           let chat = model.chats.first(where: { $0.backend.rawValue == rich && $0.title.contains("mac e2e") }) {
            model.select(.chat(chat.id))
            _ = await wait(15) { !model.timeline(chat.id).isEmpty }
            let marker = "rich-\(Int(Date.now.timeIntervalSince1970) % 100000)"
            let before = model.timeline(chat.id).count
            model.send("""
                Do not use tools or edit files. Answer in Chinese with Markdown: a level-2 heading, the quadratic \
                formula as display math ($$...$$), inline math for the discriminant, a 3-row table comparing the \
                cases Δ>0, Δ=0, Δ<0, a short numbered list, a 4-line Python snippet in a fenced block, and end \
                with the word \(marker).
                """, toChat: chat.id)
            check(await wait(240) {
                model.timeline(chat.id).count > before && model.chat(chat.id)?.state == .idle
                    && model.timeline(chat.id).contains { item in
                        if case .message(_, let author, let text, _, _, _, _) = item, author.kind == .agent, text.contains(marker) { return true }
                        return false
                    }
            }, "rich: \(rich) answered")
            await shot("rich-\(rich)")
            log("RESULT " + (failures == 0 ? "OK" : "\(failures) FAILED"))
            NSApp.terminate(nil)
            return
        }
        let conversations = ProcessInfo.processInfo.environment["HYADEX_E2E_AUTOPILOT"] != "only"
            && ProcessInfo.processInfo.environment["HYADEX_E2E_MEMBER"] != "only"
            && ProcessInfo.processInfo.environment["HYADEX_E2E_CATALOG"] != "only"

        let groupOnly = ProcessInfo.processInfo.environment["HYADEX_E2E_GROUP"] == "only"
        for backend in [AgentBackend.claude, .codex] where conversations && !groupOnly {
            guard let chat = model.chats.first(where: { $0.backend == backend && $0.title.contains("mac e2e") }) else {
                check(false, "\(backend.rawValue) test chat exists"); continue
            }
            model.select(.chat(chat.id))
            check(await wait(15) { !model.timeline(chat.id).isEmpty }, "\(backend.rawValue): history loaded (\(model.timeline(chat.id).count) items)")
            await shot("\(backend.rawValue)-1-history")
            check(await wait(15) { model.contextUsage[chat.id] != nil }, "\(backend.rawValue): context fill shown")
            let contextBefore = model.contextUsage[chat.id]
            log("\(backend.rawValue): context \(contextBefore.map { "\($0.tokens)/\($0.window) \($0.percent)%" } ?? "-"), permission \(chat.permission)")
            let marker = "e2e-\(backend.rawValue)-\(Int(Date.now.timeIntervalSince1970) % 100000)"
            let prompt = backend == .claude
                ? "Create \(marker).txt containing the word ok. Then reply in one sentence that mentions \(marker)."
                : "Run `echo \(marker)` in the shell and reply in one sentence that mentions \(marker)."
            let before = model.timeline(chat.id).count
            model.send(prompt, toChat: chat.id)
            check(await wait(2) { model.activity(forChat: chat.id) != nil }, "\(backend.rawValue): live status within 2 s of sending")
            await shot("\(backend.rawValue)-1b-sent")
            log("\(backend.rawValue): live status \(model.activity(forChat: chat.id).map { "\($0.label) \($0.detail?.prefix(60) ?? "")" } ?? "-")")
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
            check(await wait(30) { model.contextUsage[chat.id] != nil && model.contextUsage[chat.id] != contextBefore },
                  "\(backend.rawValue): context fill updated after the turn (\(model.contextUsage[chat.id]?.tokens ?? 0) tokens)")
            await shot("\(backend.rawValue)-3-done")
        }

        if !conversations {
            log("skipping chats and groups")
        } else if let group = model.groups.first(where: { $0.title.contains("mac e2e") }) {
            model.select(.group(group.id))
            _ = await wait(10) { !model.timeline(group.id).isEmpty }
            if ProcessInfo.processInfo.environment["HYADEX_E2E_GROUP_MEMBERS"] != nil {
                let handles = Set(model.group(group.id)?.participants.map(\.handle) ?? [])
                model.addMember(.codex, toGroup: group.id)
                check(await wait(30) { (model.group(group.id)?.participants.count ?? 0) == handles.count + 1 }, "group: member added")
                let added = model.group(group.id)?.participants.first { !handles.contains($0.handle) }
                log("group: added @\(added?.handle ?? "-") \(added?.sessionID ?? "no session")")
                check(added?.sessionID != nil, "group: new member has its chat")
                await shot("group-0-added")
                if let added { model.removeMember(added.handle, fromGroup: group.id) }
                check(await wait(30) { Set(model.group(group.id)?.participants.map(\.handle) ?? []) == handles }, "group: member removed")
            }
            let before = model.timeline(group.id).count
            let marker = "e2e-group-\(Int(Date.now.timeIntervalSince1970) % 100000)"
            model.send("@codex Run `echo \(marker)` in the shell, then reply with one short sentence that contains \(marker). Do not edit files.",
                       toGroup: group.id, mode: .sequential)
            check(await wait(1) { !(model.pendingSends[group.id] ?? []).isEmpty || model.timeline(group.id).count > before },
                  "group: sent message shows at once")
            check(await wait(30) { model.timeline(group.id).count > before }, "group: message posted")
            check(await wait(60) { !(model.groupLive[group.id] ?? []).isEmpty }, "group: member's live status shown")
            if let turn = model.groupLive[group.id]?.first {
                log("group: live @\(turn.author.name) \(turn.activity.map { "\($0.label) \($0.detail?.prefix(60) ?? "")" } ?? "-")")
            }
            _ = await wait(40) { model.groupLive[group.id]?.first?.steps.isEmpty == false }
            await shot("group-1-live")
            func replyIndex() -> Int? {
                model.timeline(group.id).lastIndex { item in
                    if case .message(_, let author, let text, _, _, _, _) = item, author.kind == .agent, text.contains(marker) { return true }
                    return false
                }
            }
            check(await wait(180) { replyIndex() != nil }, "group: @codex replied")
            check(await wait(10) {
                guard let index = replyIndex(), index > 0 else { return false }
                if case .tool = model.timeline(group.id)[index - 1] { return true }
                return false
            }, "group: the reply carries the work behind it")
            check(await wait(30) { (model.groupLive[group.id] ?? []).isEmpty }, "group: live status cleared after the reply")
            _ = await wait(30) { model.group(group.id)?.state == .idle }
            await shot("group-2-reply")
            if ProcessInfo.processInfo.environment["HYADEX_E2E_GROUP_APPROVAL"] != nil {
                let name = "e2e-member-\(Int(Date.now.timeIntervalSince1970) % 100000).txt"
                model.send("@claude Create \(name) containing ok, then reply in one sentence that mentions \(name).",
                           toGroup: group.id, mode: .sequential)
                check(await wait(180) { !(model.groupApprovals[group.id] ?? []).isEmpty }, "group: member approval shown inline")
                check(model.groupLive[group.id]?.contains { !$0.approvals.isEmpty } == true, "group: approval sits in the member's turn")
                check(model.group(group.id)?.participants.first { $0.handle == "claude" }?.state == .needsAction,
                      "group: member shows it is waiting")
                await shot("group-3-approval")
                if let approval = model.groupApprovals[group.id]?.first {
                    model.decide(approval, .allowed, inGroup: group.id)
                }
                check(await wait(180) {
                    model.timeline(group.id).contains { item in
                        if case .message(_, let author, let text, _, _, let receipt, _) = item, author.name == "claude",
                           text.contains(name) { return receipt?.files.first?.diff.isEmpty == false }
                        return false
                    }
                }, "group: approved member answered, and its receipt has the diff")
                await shot("group-4-approved")
            }
        } else {
            check(false, "test group exists")
        }

        if ProcessInfo.processInfo.environment["HYADEX_E2E_CATALOG"] != nil {
            check(await wait(90) { model.catalogs[.codex] != nil && model.catalogs[.claude] != nil }, "catalog: native catalogs loaded")
            let codexCatalog = model.catalog(for: .codex)
            log("catalog: codex models \(codexCatalog.models.map(\.label)) efforts \(codexCatalog.efforts.map(\.label))")
            log("catalog: claude models \(model.catalog(for: .claude).models.prefix(6).map(\.label))")
            if let chat = model.chats.first(where: { $0.backend == .codex && $0.title.contains("mac e2e") }) {
                model.select(.chat(chat.id))
                _ = await wait(15) { !model.timeline(chat.id).isEmpty }
                model.setModel("gpt-6-astra", forChat: chat.id)
                model.setEffort("high", forChat: chat.id)
                check(await wait(15) { model.chat(chat.id)?.model == "gpt-6-astra" && model.chat(chat.id)?.effort == "high" },
                      "catalog: chat set to gpt-6-astra · high")
                try? await Task.sleep(for: .seconds(5))  // let a list refresh confirm the server kept it
                check(model.chat(chat.id)?.model == "gpt-6-astra", "catalog: server kept the model")
                await shot("catalog-1-composer")
                let marker = "e2e-catalog-\(Int(Date.now.timeIntervalSince1970) % 100000)"
                model.send("Run `echo \(marker)` and reply in one sentence that mentions \(marker).", toChat: chat.id)
                if await wait(120, { pendingApproval(chat.id) != nil }), let approval = pendingApproval(chat.id) {
                    model.decide(approval, .allowed, in: chat.id)
                }
                check(await wait(180) {
                    model.timeline(chat.id).contains { item in
                        if case .message(_, let author, let text, _, _, _, _) = item, author.kind == .agent, text.contains(marker) {
                            return true
                        }
                        return false
                    }
                }, "catalog: reply arrived")
                log("catalog: chat \(chat.id)")
            }
        }

        if ProcessInfo.processInfo.environment["HYADEX_E2E_MEMBER"] != nil,
           let group = model.groups.first(where: { $0.title.contains("mac e2e") }) {
            model.select(.group(group.id))
            check(await wait(15) { model.group(group.id)?.participants.contains { $0.sessionID != nil } == true },
                  "member: member chats known")
            model.updateMember("claude", inGroup: group.id, model: "haiku", effort: "low")
            check(await wait(15) {
                let member = model.group(group.id)?.participants.first { $0.handle == "claude" }
                return member?.model == "haiku" && member?.effort == "low"
            }, "member: @claude now haiku, effort low")
            await shot("member-1-menu-state")
            let before = model.timeline(group.id).count
            model.send("@claude reply with one short sentence containing e2e-member. Do not use tools.", toGroup: group.id,
                       mode: .sequential)
            // A member blocked on an approval shows it in the group; answer it here.
            if await wait(20, { !(model.groupApprovals[group.id] ?? []).isEmpty }),
               let approval = model.groupApprovals[group.id]?.first {
                log("member: pending approval from @\(approval.handle): \(approval.title) \(approval.detail.prefix(60))")
                await shot("member-1b-approval")
                model.decide(approval, .denied, inGroup: group.id)
            }
            check(await wait(240) {
                model.timeline(group.id).dropFirst(before).contains { item in
                    if case .message(_, let author, let text, _, _, _, _) = item, author.name == "claude", text.contains("e2e-member") {
                        return true
                    }
                    return false
                }
            }, "member: @claude replied after the change")
            await shot("member-2-reply")
            if let session = model.group(group.id)?.participants.first(where: { $0.handle == "claude" })?.sessionID {
                log("member: claude session \(session)")
            }
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

    private static func uiDrive(model: AppModel, check: (Bool, String) -> Void,
                                shot: (String) async -> Void) async {
        func wait(_ seconds: Double, _ condition: () -> Bool) async -> Bool {
            let end = Date.now.addingTimeInterval(seconds)
            while Date.now < end {
                if condition() { return true }
                try? await Task.sleep(for: .milliseconds(400))
            }
            return condition()
        }
        func pause(_ seconds: Double = 0.8) async { try? await Task.sleep(for: .seconds(seconds)) }
        guard let claude = model.chats.first(where: { $0.backend == .claude && $0.title.contains("features e2e") }),
              let codex = model.chats.first(where: { $0.backend == .codex && $0.title.contains("features e2e") }) else {
            check(false, "ui: fixture chats exist")
            return
        }
        let stamp = Int(Date.now.timeIntervalSince1970) % 100000
        func idle(_ id: String) -> Bool { model.chat(id)?.state == .idle }
        func agentText(_ id: String, _ marker: String) -> Bool {
            model.timeline(id).contains { item in
                if case .message(_, let author, let text, _, _, _, _) = item, author.kind == .agent, text.contains(marker) {
                    return true
                }
                return false
            }
        }
        func noError(_ label: String) {
            check(model.errorMessage == nil, "\(label): no error (\(model.errorMessage ?? "-"))")
            model.errorMessage = nil
        }
        UIDriver.enable()
        await pause(1)
        let claudeField = "给 Claude Code 发消息", codexField = "给 Codex 发消息"
        if ["menus", "drop"].contains(ProcessInfo.processInfo.environment["HYADEX_E2E_UI"] ?? "") {
            await uiMenusAndDrop(model: model, claude: claude, codex: codex, stamp: stamp, check: check, shot: shot)
            return
        }
        if ProcessInfo.processInfo.environment["HYADEX_E2E_UI"] == "picker" {
            model.select(.chat(codex.id))
            await pause(2)
            let file = URL(fileURLWithPath: NSTemporaryDirectory()).appending(path: "picked-\(stamp).txt")
            try? "The picked word is pick-\(stamp).\n".write(to: file, atomically: true, encoding: .utf8)
            check(await UIDriver.choose("添加文件…") { UIDriver.press("添加", role: NSAccessibility.Role(rawValue: "AXMenuButton")) }, "ui: + → 添加文件…")
            check(await wait(5) { UIDriver.openPanel != nil }, "ui: the open panel appeared")
            check(await UIDriver.pick(file, log: { print("[e2e] picker: \($0)") }), "ui: a file chosen in the panel")
            check(await wait(30) { model.attachments[codex.id]?.contains { $0.name == file.lastPathComponent && $0.fileID != nil } == true },
                  "ui: the chosen file uploaded and shows in the composer")
            await shot("ui-12-picked")
            model.attachments[codex.id] = nil
            return
        }
        if ProcessInfo.processInfo.environment["HYADEX_E2E_UI"] == "settings" {
            let windowsBefore = Set(NSApp.windows.filter(\.isVisible).map(\.windowNumber))
            check(UIDriver.shortcut(",", [.command]), "ui: ⌘, handled by the menu")
            _ = await wait(5) { NSApp.windows.contains { $0.isVisible && !windowsBefore.contains($0.windowNumber) } }
            _ = UIDriver.press("通知")
            let status = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
            let expected = status == .denied ? "已关闭" : status == .notDetermined ? "尚未询问" : "已允许"
            check(await wait(8) { UIDriver.find(expected, role: nil) != nil }, "ui: settings show the system notification status (\(expected))")
            check(status != .denied || UIDriver.find("打开系统设置") != nil, "ui: with notifications off, settings offer 打开系统设置")
            if let window = NSApp.windows.first(where: { $0.isVisible && !windowsBefore.contains($0.windowNumber) }) {
                Snapshotter.captureWindow(window, to: "ui-11-settings")
            }
            return
        }
        if ProcessInfo.processInfo.environment["HYADEX_E2E_UI"] == "cmdreturn" {
            UserDefaults.standard.set(true, forKey: "sendWithCommandReturn")
            model.select(.chat(claude.id))
            await pause(2)
            UIDriver.type("Reply with exactly cr-\(stamp).", into: claudeField)
            await pause(0.6)
            UIDriver.key("\r", keyCode: 36)
            await pause(1.5)
            print("[e2e] draft after Return: \(String(reflecting: model.drafts[claude.id] ?? ""))")
            UIDriver.key("\r", keyCode: 36, modifiers: .command)
            check(await wait(120) { agentText(claude.id, "cr-\(stamp)") && idle(claude.id) }, "ui: ⌘↩ sent it")
            UserDefaults.standard.set(false, forKey: "sendWithCommandReturn")
            return
        }

        // Older history: the real button at the top of a chat opened on a short page.
        model.select(.chat(claude.id))
        _ = await wait(20) { !model.timeline(claude.id).isEmpty }
        await pause()
        let shown = model.timeline(claude.id).count
        check(UIDriver.press("加载更早的消息"), "ui: 加载更早的消息 pressed")
        check(await wait(20) { model.timeline(claude.id).count > shown }, "ui: older messages appeared (\(shown) → \(model.timeline(claude.id).count))")

        // Composer: type, Return sends.
        check(UIDriver.type("Reply with exactly ui-send-\(stamp) and nothing else.", into: claudeField), "ui: typed into the composer")
        await pause(0.4)
        UIDriver.key("\r", keyCode: 36)
        check(await wait(180) { agentText(claude.id, "ui-send-\(stamp)") && idle(claude.id) }, "ui: Return sent the message and Claude answered")

        // Slash suggestions with the keyboard.
        UIDriver.type("/comp", into: claudeField)
        check(await wait(30) { UIDriver.find("/compact") != nil }, "ui: typing /comp shows /compact in the suggestions")
        await shot("ui-1-slash")
        UIDriver.key("\r", keyCode: 36)
        check(await wait(5) { (model.drafts[claude.id] ?? "") == "/compact " }, "ui: Return completed it to “/compact ” (\(model.drafts[claude.id] ?? "-"))")
        UIDriver.type("", into: claudeField)
        model.drafts[claude.id] = ""

        // Side chat panel.
        check(UIDriver.press("侧问"), "ui: toolbar → 侧问")
        check(await wait(10) { UIDriver.find("不打断代理") != nil }, "ui: side panel opened")
        _ = await wait(15) { model.sideChats[claude.id] != nil }
        let before = model.sideChats[claude.id]?.exchanges.count ?? 0
        check(UIDriver.type("Answer in five words: what did I just ask you to reply?", into: "问点什么"), "ui: typed a side question")
        await pause(0.4)
        check(UIDriver.press("发送侧问"), "ui: side question sent with its button")
        check(await wait(180) {
            guard let side = model.sideChats[claude.id], side.exchanges.count > before else { return false }
            return side.exchanges.last?.status == .completed
        }, "ui: side answer shown (\(model.sideChats[claude.id]?.exchanges.last?.answer?.prefix(60) ?? "-"))")
        await shot("ui-2-side")
        check(UIDriver.press("关闭"), "ui: side panel closed with its button")
        check(await wait(5) { model.sideChatOpen == nil }, "ui: side panel gone")

        // Goal sheet and goal bar.
        check(UIDriver.press("设定目标"), "ui: toolbar → 设定目标")
        check(await wait(5) { UIDriver.sheetShown }, "ui: goal sheet shown")
        check(UIDriver.type("The user has typed the word kiwi in this chat.", into: "例如"), "ui: typed the condition")
        await pause(0.4)
        check(UIDriver.press("开始"), "ui: 开始 pressed")
        check(await wait(90) { model.goals[claude.id]?.status == .active }, "ui: goal bar shows the active goal")
        await shot("ui-3-goal")
        check(UIDriver.press("清除目标"), "ui: goal bar 清除 pressed")
        check(await wait(120) { model.goals[claude.id].map { !$0.isOpen } ?? true }, "ui: goal cleared")
        _ = await wait(120) { idle(claude.id) }
        noError("ui: goal")

        // Context ring → compact now.
        check(UIDriver.press("上下文已用"), "ui: context ring pressed")
        check(await wait(5) { UIDriver.find("立即压缩上下文") != nil }, "ui: popover offers 立即压缩上下文")
        check(UIDriver.press("立即压缩上下文"), "ui: 立即压缩上下文 pressed")
        check(await wait(180) {
            model.timeline(claude.id).last { if case .divider(_, "上下文已压缩") = $0 { true } else { false } } != nil && idle(claude.id)
        }, "ui: Claude compacted the conversation")
        noError("ui: compact")

        // Menu shortcuts: unread, fork, new job for this chat.
        check(UIDriver.shortcut("u", [.command, .shift]), "ui: ⇧⌘U handled by the menu")
        check(await wait(5) { model.chat(claude.id)?.unread == true }, "ui: chat marked unread")
        model.select(.chat(codex.id)); model.select(.chat(claude.id))
        let knownChats = Set(model.chats.map(\.id))
        check(UIDriver.shortcut("d", [.command, .shift]), "ui: ⇧⌘D handled by the menu")
        check(await wait(60) { model.chats.contains { !knownChats.contains($0.id) } }, "ui: fork created and opened")
        if let fork = model.chats.first(where: { !knownChats.contains($0.id) }) { model.delete(.chat(fork.id)) }
        _ = await wait(15) { model.chats.allSatisfy { knownChats.contains($0.id) } }
        model.select(.chat(claude.id))
        await pause()
        check(UIDriver.shortcut("j", [.command, .shift]), "ui: ⇧⌘J opens a job for this chat")
        check(await wait(5) { UIDriver.sheetShown }, "ui: job sheet shown")
        UIDriver.type("ui job \(stamp)", into: "例如：每日依赖更新检查")
        UIDriver.type("Reply ok.", into: "到时间时发给代理的内容")
        await pause(0.4)
        check(UIDriver.find("下次运行") != nil, "ui: the sheet previews the next run")
        await shot("ui-4-job")
        check(UIDriver.press("创建"), "ui: 创建 pressed")
        check(await wait(15) { model.jobs.contains { $0.title == "ui job \(stamp)" && $0.sessionID == claude.id } },
              "ui: job created for this chat")
        if let job = model.jobs.first(where: { $0.title == "ui job \(stamp)" }) { model.deleteJob(job.id) }

        // New chat sheet: server folders, create; then rename, archive, delete with confirmation.
        check(UIDriver.press("新建聊天"), "ui: + pressed")
        check(await wait(5) { UIDriver.sheetShown }, "ui: new chat sheet shown")
        UIDriver.type("/tmp/hyadex-f", into: "~/Code/project")
        check(await wait(10) { UIDriver.find("hyadex-features-e2e", role: .button) != nil }, "ui: the sheet lists the server's folders")
        await shot("ui-5-new-chat")
        check(UIDriver.press("hyadex-features-e2e"), "ui: a folder row pressed")
        check(await wait(5) { UIDriver.text(inField: "~/Code/project")?.contains("hyadex-features-e2e") == true },
              "ui: the folder filled the path (\(UIDriver.text(inField: "~/Code/project") ?? "-"))")
        let known = Set(model.chats.map(\.id))
        check(UIDriver.press("创建"), "ui: 创建 pressed")
        check(await wait(20) { model.chats.contains { !known.contains($0.id) } }, "ui: chat created on the server")
        guard let scratch = model.chats.first(where: { !known.contains($0.id) }) else { return }
        model.select(.chat(scratch.id))
        await pause()
        check(UIDriver.shortcut("r", [.command, .shift]), "ui: ⇧⌘R handled by the menu")
        check(await wait(5) { UIDriver.sheetShown }, "ui: rename sheet shown")
        UIDriver.type("ui scratch \(stamp)", into: "名称")
        await pause(0.4)
        check(UIDriver.press("保存"), "ui: 保存 pressed")
        try? await Task.sleep(for: .seconds(6))
        check(model.chat(scratch.id)?.title == "ui scratch \(stamp)", "ui: renamed (server list)")
        check(UIDriver.shortcut("e", [.command, .shift]), "ui: ⇧⌘E handled by the menu")
        try? await Task.sleep(for: .seconds(6))
        check(model.chat(scratch.id)?.archived == true, "ui: archived (server list)")
        model.select(.chat(scratch.id))
        await pause()
        check(UIDriver.shortcut("e", [.command, .shift]), "ui: ⇧⌘E again (取消归档)")
        try? await Task.sleep(for: .seconds(6))
        check(model.chat(scratch.id)?.archived == false, "ui: unarchived")
        model.select(.chat(scratch.id))
        await pause()
        let deleteHandled = UIDriver.shortcut("\u{8}", [.command], keyCode: 51) || UIDriver.shortcut("\u{7F}", [.command], keyCode: 51)
        check(deleteHandled, "ui: ⌘⌫ handled by the menu")
        check(await wait(5) { UIDriver.find("删除聊天") != nil }, "ui: deletion asks for confirmation")
        await shot("ui-6-confirm")
        check(UIDriver.press("取消"), "ui: 取消 pressed")
        try? await Task.sleep(for: .seconds(3))
        check(model.chat(scratch.id) != nil, "ui: cancelling kept the chat")
        _ = UIDriver.shortcut("\u{8}", [.command], keyCode: 51) || UIDriver.shortcut("\u{7F}", [.command], keyCode: 51)
        _ = await wait(5) { UIDriver.find("删除聊天") != nil }
        check(UIDriver.press("删除"), "ui: 删除 pressed")
        check(await wait(15) { model.chat(scratch.id) == nil }, "ui: chat deleted")
        noError("ui: manage")

        // Queue through the composer and the queue strip.
        model.select(.chat(codex.id))
        _ = await wait(15) { !model.timeline(codex.id).isEmpty }
        for message in model.queues[codex.id] ?? [] { model.withdrawQueued(message.id, in: codex.id) }
        await pause()
        UIDriver.type("Write the integers from 1 to 3000, one per line, then the word uq0-\(stamp).", into: codexField)
        UIDriver.key("\r", keyCode: 36)
        check(await wait(60) { model.chat(codex.id)?.state == .running }, "ui: long answer running")
        UIDriver.type("Reply with exactly uq1-\(stamp).", into: codexField)
        UIDriver.key("\r", keyCode: 36)
        check(await wait(30) { (model.queues[codex.id]?.count ?? 0) == 1 }, "ui: Return while busy queued the message")
        UIDriver.key("\u{1B}", keyCode: 53)
        check(await wait(60) { model.queues[codex.id]?.first?.hold == .stopped && idle(codex.id) }, "ui: Esc stopped the turn and held the queue")
        check(await wait(5) { UIDriver.find("继续发送") != nil }, "ui: the queue strip offers 继续发送")
        await shot("ui-7-held")
        check(UIDriver.press("继续发送"), "ui: 继续发送 pressed")
        check(await wait(180) { agentText(codex.id, "uq1-\(stamp)") && idle(codex.id) }, "ui: the held message was sent")
        UIDriver.type("Write the integers from 1 to 3000, one per line, then the word uq2-\(stamp).", into: codexField)
        UIDriver.key("\r", keyCode: 36)
        _ = await wait(60) { model.chat(codex.id)?.state == .running }
        UIDriver.type("Reply with exactly uq3-\(stamp).", into: codexField)
        UIDriver.key("\r", keyCode: 36)
        _ = await wait(30) { (model.queues[codex.id]?.count ?? 0) == 1 }
        check(UIDriver.press("立即发送"), "ui: 立即发送 pressed")
        check(await wait(180) { agentText(codex.id, "uq3-\(stamp)") && idle(codex.id) }, "ui: Force Send answered")
        noError("ui: queue")

        // Search field → hit row.
        model.switchSidebar(to: .work)
        check(UIDriver.type("ui-send-\(stamp)", into: "搜索聊天和消息"), "ui: typed into the sidebar search")
        check(await wait(30) { UIDriver.find("打开到这条消息") != nil }, "ui: a message hit is listed")
        await shot("ui-8-search")
        check(UIDriver.press("打开到这条消息"), "ui: hit pressed")
        check(await wait(20) { model.selection == .chat(claude.id) && model.timelineFocus[claude.id] != nil }, "ui: the chat opened at the hit")
        UIDriver.type("", into: "搜索聊天和消息")

        // Details popover: MCP and subagents.
        check(UIDriver.press("详情"), "ui: 详情 pressed")
        check(await wait(30) { UIDriver.find("MCP 服务器") != nil && UIDriver.find("子代理") != nil }, "ui: details show MCP servers and subagents")
        await shot("ui-9-details")
        _ = UIDriver.press("详情")

        // Emergency: approve with the card's button, acknowledge with the banner's.
        UIDriver.type("This is a test drill requested by the user. Use your Hyadex emergency alert helper to raise one "
                         + "emergency alert with exactly this message: ui drill \(stamp). Then reply done.", into: claudeField)
        UIDriver.key("\r", keyCode: 36)
        let raised = await wait(240) {
            if UIDriver.find("允许") != nil { _ = UIDriver.press("允许") }
            return model.emergencies.contains { $0.message.contains("ui drill \(stamp)") }
        }
        check(raised, "ui: emergency banner shown")
        await shot("ui-10-emergency")
        check(UIDriver.press("确认"), "ui: 确认 pressed on the banner")
        check(await wait(10) { !model.emergencies.contains { $0.message.contains("ui drill \(stamp)") } }, "ui: banner gone")
        _ = await wait(120) { if UIDriver.find("允许") != nil { _ = UIDriver.press("允许") }; return idle(claude.id) }

        // Settings: ⌘↩ to send, then the notification status page.
        UserDefaults.standard.set(true, forKey: "sendWithCommandReturn")
        model.select(.chat(claude.id))
        await pause()
        UIDriver.type("Reply with exactly cr-\(stamp).", into: claudeField)
        await pause(0.6)
        UIDriver.key("\r", keyCode: 36)
        await pause(1.5)
        check((model.drafts[claude.id] ?? "").hasPrefix("Reply with exactly cr-") && (model.drafts[claude.id] ?? "").hasSuffix("\n")
              && !agentText(claude.id, "cr-\(stamp)"),
              "ui: with ⌘↩ to send, Return adds a line")
        UIDriver.key("\r", keyCode: 36, modifiers: .command)
        check(await wait(180) { agentText(claude.id, "cr-\(stamp)") && idle(claude.id) }, "ui: ⌘↩ sent it")
        UserDefaults.standard.set(false, forKey: "sendWithCommandReturn")
        let windowsBefore = Set(NSApp.windows.filter(\.isVisible).map(\.windowNumber))
        check(UIDriver.shortcut(",", [.command]), "ui: ⌘, handled by the menu")
        check(await wait(5) { NSApp.windows.contains { $0.isVisible && !windowsBefore.contains($0.windowNumber) } },
              "ui: settings window opened")
        _ = UIDriver.press("通知")
        let status = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
        let expected = status == .denied ? "已关闭" : status == .notDetermined ? "尚未询问" : "已允许"
        check(await wait(8) { UIDriver.find(expected, role: nil) != nil }, "ui: settings show the system notification status (\(expected))")
        if status == .denied {
            check(UIDriver.find("打开系统设置") != nil, "ui: with notifications off, settings offer 打开系统设置")
        }
        let settingsWindow = NSApp.windows.first { $0.isVisible && !windowsBefore.contains($0.windowNumber) }
        if let settingsWindow { Snapshotter.captureWindow(settingsWindow, to: "ui-11-settings") }
        settingsWindow?.close()

        // Menus (right-click and a menu button), dropping a file, and the paperclip's panel.
        await uiMenusAndDrop(model: model, claude: claude, codex: codex, stamp: stamp, check: check, shot: shot)
        model.select(.chat(codex.id))
        await pause()
        check(await UIDriver.choose("添加文件…") { UIDriver.press("添加", role: NSAccessibility.Role(rawValue: "AXMenuButton")) }, "ui: + → 添加文件…")
        check(await wait(5) { UIDriver.openPanel != nil }, "ui: the system file panel opened")
        UIDriver.openPanel?.cancel(nil)
        check(await wait(5) { UIDriver.openPanel == nil }, "ui: the panel closed again")

        let settings = await UNUserNotificationCenter.current().notificationSettings()
        print("[e2e] notifications: authorization status \(settings.authorizationStatus.rawValue) (0 not asked, 1 denied, 2 allowed, 3 provisional)")
        noError("ui: emergency")
    }

    /// HYADEX_E2E_UI=gaps: job outcomes, token totals, the chat's advanced
    /// settings, the group's settings, "在编辑器中打开" and ⌘V attachments, on the
    /// fixtures `e2e_gaps.py` creates ("gaps e2e claude/codex/group").
    private static func uiGaps(model: AppModel, check: (Bool, String) -> Void,
                               shot: (String) async -> Void) async {
        func wait(_ seconds: Double, _ condition: () -> Bool) async -> Bool {
            let end = Date.now.addingTimeInterval(seconds)
            while Date.now < end {
                if condition() { return true }
                try? await Task.sleep(for: .milliseconds(400))
            }
            return condition()
        }
        func pause(_ seconds: Double = 0.8) async { try? await Task.sleep(for: .seconds(seconds)) }
        func agentText(_ id: String, _ test: (String) -> Bool) -> Bool {
            model.timeline(id).contains { item in
                if case .message(_, let author, let text, _, _, _, _) = item, author.kind == .agent, test(text) { return true }
                return false
            }
        }
        guard let claude = model.chats.first(where: { $0.title == "gaps e2e claude" }),
              let codex = model.chats.first(where: { $0.title == "gaps e2e codex" }),
              let group = model.groups.first(where: { $0.title == "gaps e2e group" }) else {
            check(false, "gaps: fixtures exist (run e2e_gaps.py first)")
            return
        }
        let stamp = Int(Date.now.timeIntervalSince1970) % 100000

        // 4. Job outcomes in the jobs table.
        model.select(.jobs)
        await pause(2)
        for (title, text) in [("gaps e2e ok", "成功"), ("gaps e2e fails", "失败"), ("gaps e2e stopped", "已停止")] {
            let job = model.jobs.first { $0.title == title }
            check(job?.lastResultText == text, "gaps: job \(title) shows \(text) (\(job?.lastResultText ?? "-"))")
        }
        check(model.jobs.first { $0.title == "gaps e2e fails" }?.lastError?.contains("unrecognized_model") == true,
              "gaps: the failed job carries the server's reason")
        check(UIDriver.find("失败原因") != nil, "gaps: the table shows the failure reason on hover")
        await shot("gaps-01-jobs")

        // 2. Token totals in the chat's details.
        model.select(.chat(claude.id))
        await pause(2)
        let totals = model.chat(claude.id)?.tokenTotals
        check((totals?.total ?? 0) > 0, "gaps: the chat has server token totals (\(totals?.total ?? 0))")
        check(UIDriver.press("详情"), "gaps: details opened")
        check(await wait(5) { UIDriver.find("缓存", role: nil) != nil }, "gaps: details show the token totals with the cached part")
        await shot("gaps-01b-details")

        // 7. Advanced settings: change the system prompt and the automatic title, then use them.
        check(UIDriver.press("高级设置"), "gaps: 高级设置… pressed")
        check(await wait(8) { UIDriver.find("Always answer in French.", role: nil) != nil },
              "gaps: the sheet loaded the server's system prompt")
        await shot("gaps-02-advanced-sheet")
        await shot("gaps-02-advanced")
        check(UIDriver.type("Always answer in German.", into: "系统提示词"), "gaps: typed a new system prompt")
        check(UIDriver.press("自动起标题", role: .checkBox), "gaps: automatic title toggled")
        await pause()
        check(UIDriver.press("保存"), "gaps: 保存 pressed")
        check(await wait(8) { model.settingsEditor == nil }, "gaps: the sheet closed after saving")
        var stored = try? await model.chatSettings(claude.id)
        check(stored?.systemPrompt == "Always answer in German." && stored?.autoTitle == true,
              "gaps: the server stored the prompt and automatic title (\(stored?.systemPrompt ?? "-"), \(stored?.autoTitle == true))")
        model.drafts[claude.id] = ""
        UIDriver.type("Say hello in one word.", into: "给 Claude Code 发消息")
        await pause(0.6)
        UIDriver.key("\r", keyCode: 36)
        check(await wait(180) { agentText(claude.id) { $0.localizedCaseInsensitiveContains("hallo") } && model.chat(claude.id)?.state == .idle },
              "gaps: Claude answered in German, following the new system prompt")
        // Clear the prompt again through the sheet.
        model.settingsEditor = .chat(claude.id)
        check(await wait(8) { UIDriver.find("Always answer in German.", role: nil) != nil }, "gaps: sheet reopened")
        UIDriver.type("", into: "系统提示词")
        await pause()
        UIDriver.press("保存")
        _ = await wait(8) { model.settingsEditor == nil }
        stored = try? await model.chatSettings(claude.id)
        check(stored?.systemPrompt == "", "gaps: an empty prompt clears it on the server")

        // The toolbar holds the side chat and the goal.
        check(UIDriver.press("侧问"), "gaps: toolbar → 侧问")
        check(await wait(8) { model.sideChatOpen == claude.id }, "gaps: the side chat opened from the toolbar")
        model.toggleSideChat(claude.id)
        check(UIDriver.press("设定目标"), "gaps: toolbar → 设定目标")
        check(await wait(5) { model.goalEditor != nil }, "gaps: the goal sheet opened from the toolbar")
        await shot("gaps-02c-goal-from-toolbar")
        model.goalEditor = nil
        await pause()

        // 1. 在编辑器中打开: the changed file of the first turn, resolved through the diff's repository root.
        var opened: URL?
        ChangedFileOpener.openURL = { opened = $0 }
        model.showChanges = true
        let changed = model.selectedChanges.files.first { $0.path.hasSuffix("main.py") }
        model.selectedFilePath = changed?.path
        await pause(1.5)
        check(UIDriver.press("在编辑器中打开"), "gaps: 在编辑器中打开 pressed")
        check(await wait(5) { opened != nil }, "gaps: a file was opened")
        let openedPath = opened?.resolvingSymlinksInPath().path ?? "-"
        check(openedPath.hasSuffix("/hyadex-gaps-e2e/app/main.py") && FileManager.default.fileExists(atPath: openedPath),
              "gaps: it is the changed file on disk (\(openedPath))")
        if let changed {
            let copy = try? await model.serverCopy(of: changed)
            check(copy.flatMap { String(data: $0, encoding: .utf8) }?.contains("# edited") == true,
                  "gaps: a remote client gets the server's copy of the same file")
        }
        await shot("gaps-03-changes")
        model.showChanges = false

        // 5. ⌘V: a screenshot on the clipboard becomes an attachment; text still pastes as text.
        let pasteboard = NSPasteboard.general
        let saved = (pasteboard.pasteboardItems ?? []).map { item -> NSPasteboardItem in
            let copy = NSPasteboardItem()
            for type in item.types { if let data = item.data(forType: type) { copy.setData(data, forType: type) } }
            return copy
        }
        defer {
            pasteboard.clearContents()
            pasteboard.writeObjects(saved)
        }
        let marker = "PASTE\(stamp)"
        let image = NSImage(size: NSSize(width: 640, height: 180), flipped: false) { rect in
            NSColor.white.setFill()
            rect.fill()
            (marker as NSString).draw(at: NSPoint(x: 30, y: 50), withAttributes: [
                .font: NSFont.boldSystemFont(ofSize: 72), .foregroundColor: NSColor.black])
            return true
        }
        let png = image.tiffRepresentation.flatMap(NSBitmapImageRep.init(data:))?.representation(using: .png, properties: [:])
        pasteboard.clearContents()
        pasteboard.setData(png, forType: .png)
        func commandV() {
            guard let window = UIDriver.mainWindow(),
                  let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: .command,
                                               timestamp: ProcessInfo.processInfo.systemUptime,
                                               windowNumber: window.windowNumber, context: nil, characters: "v",
                                               charactersIgnoringModifiers: "v", isARepeat: false, keyCode: 9) else { return }
            NSApp.postEvent(event, atStart: false)
        }
        model.attachments[claude.id] = nil
        UIDriver.type("", into: "给 Claude Code 发消息")
        await pause(0.6)
        commandV()
        check(await wait(30) { model.attachments[claude.id]?.contains { $0.name.hasPrefix("粘贴的图片") && $0.fileID != nil } == true },
              "gaps: ⌘V attached the clipboard image and it uploaded")
        check((model.drafts[claude.id] ?? "").isEmpty, "gaps: the image did not paste as text")
        await shot("gaps-04-pasted")
        pasteboard.clearContents()
        pasteboard.setString("What text is in the attached image? Reply with just that text.", forType: .string)
        // The posted ⌘V must pass the interceptor untouched.
        commandV()
        await pause(1)
        check(model.attachments[claude.id]?.count == 1, "gaps: ⌘V with text added no attachment")
        // In the background the window is not key, so the menu's Paste has no
        // target; send it where the menu would: the composer's field editor.
        UIDriver.type("", into: "给 Claude Code 发消息")
        await pause(0.4)
        (UIDriver.mainWindow()?.firstResponder as? NSTextView)?.paste(nil)
        check(await wait(5) { (model.drafts[claude.id] ?? "").contains("attached image") }, "gaps: ⌘V with text pastes text")
        await pause(0.6)
        UIDriver.key("\r", keyCode: 36)
        // Claude reads the uploaded image with its Read tool, which asks first.
        let answered = await wait(180) {
            if model.timeline(claude.id).pendingApproval != nil { _ = UIDriver.press("本会话内始终允许") }
            return agentText(claude.id) { $0.uppercased().contains(marker) } && model.chat(claude.id)?.state == .idle
        }
        check(answered, "gaps: Claude read the pasted image (\(marker))")

        // 6. Group settings: responder, hand-off limit and brief through the sheet.
        model.select(.group(group.id))
        await pause(2)
        check(UIDriver.press("详情"), "gaps: group details opened")
        check(await wait(5) { UIDriver.find("4 次", role: nil) != nil }, "gaps: details show the real hand-off limit (4 次)")
        check(UIDriver.press("编辑设置"), "gaps: 编辑设置… pressed")
        check(await wait(8) { UIDriver.find("讨论组设置", role: nil) != nil }, "gaps: group settings sheet open")
        if let stepper = UIDriver.find("接力上限", role: .incrementor) {
            _ = UIDriver.call(stepper, "accessibilityPerformIncrement")
        }
        check(await UIDriver.choose("@codex") { UIDriver.press("默认回答者", role: .popUpButton) }, "gaps: default responder → @codex")
        UIDriver.type("Codename: AMARANTH-\(stamp).", into: "共享摘要")
        await pause()
        await shot("gaps-05-group-settings")
        check(UIDriver.press("保存"), "gaps: group 保存 pressed")
        check(await wait(8) { model.settingsEditor == nil }, "gaps: group sheet closed")
        let saved2 = model.group(group.id)?.settings
        check(saved2?.maxAgentHops == 5 && saved2?.defaultResponder == "codex" && saved2?.brief.contains("AMARANTH") == true,
              "gaps: the server stored hops 5, @codex and the brief (\(String(describing: saved2)))")
        await pause(5)
        check(model.group(group.id)?.settings.maxAgentHops == 5, "gaps: still 5 after a list refresh")
        // Restore the defaults through the same sheet: first member, 4 hand-offs.
        model.settingsEditor = .group(group.id)
        _ = await wait(8) { UIDriver.find("讨论组设置", role: nil) != nil }
        if let stepper = UIDriver.find("接力上限", role: .incrementor) {
            _ = UIDriver.call(stepper, "accessibilityPerformDecrement")
        }
        _ = await UIDriver.choose("@claude（第一位）") { UIDriver.press("默认回答者", role: .popUpButton) }
        UIDriver.press("保存")
        _ = await wait(8) { model.settingsEditor == nil }
        check(model.group(group.id)?.settings.maxAgentHops == 4 && model.group(group.id)?.settings.defaultResponder == nil,
              "gaps: defaults restored")
        _ = codex
        check(model.errorMessage == nil, "gaps: no error banner (\(model.errorMessage ?? "-"))")
    }

    /// Right-click and menu-button menus, and dropping a file on the conversation.
    private static func uiMenusAndDrop(model: AppModel, claude: ChatSummary, codex: ChatSummary, stamp: Int,
                                       check: (Bool, String) -> Void, shot: (String) async -> Void) async {
        func wait(_ seconds: Double, _ condition: () -> Bool) async -> Bool {
            let end = Date.now.addingTimeInterval(seconds)
            while Date.now < end {
                if condition() { return true }
                try? await Task.sleep(for: .milliseconds(400))
            }
            return condition()
        }
        func agentText(_ id: String, _ marker: String) -> Bool {
            model.timeline(id).contains { item in
                if case .message(_, let author, let text, _, _, _, _) = item, author.kind == .agent, text.contains(marker) {
                    return true
                }
                return false
            }
        }
        // Right-click a chat in the sidebar → 标为未读.
        model.select(.chat(claude.id))
        try? await Task.sleep(for: .seconds(2))
        model.markRead(claude.id)
        try? await Task.sleep(for: .seconds(1))
        check(await UIDriver.choose("标为未读") { UIDriver.showMenu(claude.title) }, "ui: right-click menu → 标为未读 chosen")
        check(await wait(5) { model.chat(claude.id)?.unread == true }, "ui: the chat shows unread")
        try? await Task.sleep(for: .seconds(5))
        check(model.chat(claude.id)?.unread == true, "ui: still unread after a list refresh (server)")
        model.markRead(claude.id)

        // The permission menu button in the composer → Accept edits, then back to Manual.
        let menuButton = NSAccessibility.Role(rawValue: "AXMenuButton")
        let original = model.chat(claude.id)?.permission ?? "Manual"
        check(await UIDriver.choose("Accept edits") { UIDriver.press(original, role: menuButton) },
              "ui: permission menu → Accept edits chosen")
        check(await wait(10) { model.chat(claude.id)?.permission == "Accept edits" }, "ui: the chat's permission changed")
        try? await Task.sleep(for: .seconds(5))
        check(model.chat(claude.id)?.permission == "Accept edits", "ui: the server kept it (list refresh)")
        check(await UIDriver.choose(original) { UIDriver.press("Accept edits", role: menuButton) }, "ui: permission menu → back to \(original)")
        check(await wait(10) { model.chat(claude.id)?.permission == original }, "ui: permission restored")

        // Drop a file on the conversation, then ask about it.
        model.select(.chat(codex.id))
        try? await Task.sleep(for: .seconds(2))
        let file = URL(fileURLWithPath: NSTemporaryDirectory()).appending(path: "dropped-\(stamp).txt")
        try? "The dropped word is drop-\(stamp).\n".write(to: file, atomically: true, encoding: .utf8)
        guard let window = UIDriver.mainWindow(), let frame = window.contentView?.frame else { return }
        let point = NSPoint(x: frame.maxX - 360, y: frame.midY)
        check(UIDriver.drop([file], at: point), "ui: the conversation accepted the dropped file")
        check(await wait(30) { model.attachments[codex.id]?.contains { $0.name == file.lastPathComponent && $0.fileID != nil } == true },
              "ui: the dropped file uploaded and shows in the composer")
        await shot("ui-13-dropped")
        UIDriver.type("What is the dropped word in the attached file? Reply with just the word.", into: "给 Codex 发消息")
        try? await Task.sleep(for: .milliseconds(600))
        UIDriver.key("\r", keyCode: 36)
        check(await wait(240) { agentText(codex.id, "drop-\(stamp)") && model.chat(codex.id)?.state == .idle },
              "ui: Codex read the dropped file")
    }

    private static func extras(model: AppModel, check: (Bool, String) -> Void,
                               shot: (String) async -> Void) async {
        func wait(_ seconds: Double, _ condition: () -> Bool) async -> Bool {
            let end = Date.now.addingTimeInterval(seconds)
            while Date.now < end {
                if condition() { return true }
                try? await Task.sleep(for: .milliseconds(500))
            }
            return condition()
        }
        guard let claude = model.chats.first(where: { $0.backend == .claude && $0.title.contains("features e2e") }),
              let codex = model.chats.first(where: { $0.backend == .codex && $0.title.contains("features e2e") }) else {
            check(false, "extras: fixture chats exist")
            return
        }
        let stamp = Int(Date.now.timeIntervalSince1970) % 100000
        func idle(_ id: String) -> Bool { model.chat(id)?.state == .idle }
        func noError(_ label: String) {
            check(model.errorMessage == nil, "\(label): no error (\(model.errorMessage ?? "-"))")
            model.errorMessage = nil
        }
        func agentText(_ id: String, _ marker: String) -> Bool {
            model.timeline(id).contains { item in
                if case .message(_, let author, let text, _, _, _, _) = item, author.kind == .agent, text.contains(marker) {
                    return true
                }
                return false
            }
        }
        func approvePending(_ id: String) {
            if let approval = model.timeline(id).pendingApproval { model.decide(approval.id, .allowed, in: id) }
        }

        // Unread: stays unread while open, clears on the next visit.
        model.select(.chat(claude.id))
        _ = await wait(15) { !model.timeline(claude.id).isEmpty }
        try? await Task.sleep(for: .seconds(1))  // opening marks it read; let that land first
        model.markUnread(claude.id)
        try? await Task.sleep(for: .seconds(6))  // one list refresh
        check(model.chat(claude.id)?.unread == true, "unread: marked, and kept while the chat is open")
        model.select(.chat(codex.id))
        model.select(.chat(claude.id))
        try? await Task.sleep(for: .seconds(6))
        check(model.chat(claude.id)?.unread == false, "unread: reading it again clears it")
        noError("unread")

        // Full-text search: a fresh message, then one far back in history.
        let marker = "srch\(stamp)"
        model.send("Reply with exactly \(marker) and nothing else.", toChat: claude.id)
        check(await wait(180) { agentText(claude.id, marker) && idle(claude.id) }, "search: marker message answered")
        // A reply watched to the end stays read after leaving; one that
        // finishes while the user is elsewhere gets the dot.
        model.select(.chat(codex.id))
        try? await Task.sleep(for: .seconds(6))  // one list refresh
        check(model.chat(claude.id)?.unread == false, "unread: a reply watched to the end stays read after leaving")
        model.select(.chat(claude.id))
        let away = "away\(stamp)"
        model.send("Reply with exactly \(away) and nothing else.", toChat: claude.id)
        _ = await wait(30) { model.chat(claude.id)?.state == .running }
        model.select(.chat(codex.id))
        check(await wait(180) { model.chat(claude.id)?.unread == true }, "unread: a reply finished elsewhere shows the dot")
        model.select(.chat(claude.id))
        check(await wait(30) { agentText(claude.id, away) && model.chat(claude.id)?.unread == false },
              "unread: opening it shows the reply and clears the dot")
        noError("unread after a reply")
        model.switchSidebar(to: .work)
        model.searchText = marker
        check(await wait(30) { model.searchHits.contains { $0.sessionID == claude.id } }, "search: hit found by content")
        await shot("extras-1-search")
        if let hit = model.searchHits.first(where: { $0.sessionID == claude.id }) {
            model.open(hit)
            check(await wait(20) {
                guard let focus = model.timelineFocus[claude.id]?.split(separator: "|").first else { return false }
                return model.timeline(claude.id).contains { $0.id == String(focus) }
            }, "search: chat opened at the hit, on a shown message")
        }
        model.searchText = "cskill"
        let oldHit = await wait(30) { model.searchHits.contains { $0.sessionID == claude.id } }
        check(oldHit, "search: an old message is found")
        if let hit = model.searchHits.first(where: { $0.sessionID == claude.id }) {
            model.open(hit)
            check(await wait(60) {
                guard let focus = model.timelineFocus[claude.id]?.split(separator: "|").first else { return false }
                return model.timeline(claude.id).contains { $0.id == String(focus) }
            }, "search: older history loaded back to the hit (seq \(hit.seq))")
            await shot("extras-2-search-old")
        }
        model.searchText = ""
        noError("search")

        // Side chat on both providers.
        for chat in [claude, codex] {
            model.select(.chat(chat.id))
            model.toggleSideChat(chat.id)
            check(await wait(30) { model.sideChats[chat.id] != nil || model.sideChatErrors[chat.id] != nil },
                  "side chat \(chat.backend.rawValue): opened (\(model.sideChatErrors[chat.id] ?? "ok"))")
            let before = model.sideChats[chat.id]?.exchanges.count ?? 0
            model.askSide("In one short sentence: what is this conversation mostly about?", inChat: chat.id)
            check(await wait(180) {
                guard let side = model.sideChats[chat.id], side.exchanges.count > before, let last = side.exchanges.last
                else { return false }
                return last.status != .running
            }, "side chat \(chat.backend.rawValue): answered")
            let last = model.sideChats[chat.id]?.exchanges.last
            check(last?.status == .completed && !(last?.answer ?? "").isEmpty,
                  "side chat \(chat.backend.rawValue): \(last?.status.rawValue ?? "-") \(String((last?.answer ?? model.sideChatErrors[chat.id] ?? "").prefix(80)))")
            await shot("extras-3-side-\(chat.backend.rawValue)")
            check(!model.timeline(chat.id).contains { item in
                if case .message(_, _, let text, _, _, _, _) = item { text.contains("mostly about") } else { false }
            }, "side chat \(chat.backend.rawValue): the main conversation is untouched")
            model.sideChatOpen = nil
        }

        // Fork: the copy continues with the original's context.
        for chat in [codex, claude] {
            let known = Set(model.chats.map(\.id))
            model.select(.chat(chat.id))
            _ = await wait(60) { idle(chat.id) }
            model.fork(chat.id)
            check(await wait(60) { model.chats.contains { !known.contains($0.id) } }, "fork \(chat.backend.rawValue): created")
            guard let copy = model.chats.first(where: { !known.contains($0.id) }) else { continue }
            check(await wait(20) { model.selection == .chat(copy.id) && !model.timeline(copy.id).isEmpty },
                  "fork \(chat.backend.rawValue): opened with the copied conversation")
            let recall = chat.backend == .claude ? marker : "attach-"
            model.send("Earlier in this conversation there was a word starting with \"\(recall.prefix(6))\". "
                       + "Reply with that exact word only.", toChat: copy.id)
            check(await wait(240) { approvePending(copy.id); return agentText(copy.id, recall) && idle(copy.id) },
                  "fork \(chat.backend.rawValue): remembers the original conversation")
            await shot("extras-4-fork-\(chat.backend.rawValue)")
            model.delete(.chat(copy.id))
            _ = await wait(15) { model.chat(copy.id) == nil }
        }
        noError("fork")

        // Subagents: Claude starts one through its Task tool.
        model.select(.chat(claude.id))
        model.send("Use your Task tool to start exactly one subagent that lists the files in the working folder "
                   + "and reports how many there are. Then reply with the count.", toChat: claude.id)
        let started = await wait(240) {
            approvePending(claude.id)
            model.refreshSubagents(claude.id)
            return !(model.subagents[claude.id] ?? []).isEmpty
        }
        check(started, "subagents claude: listed (\(model.subagents[claude.id]?.map { "\($0.name)=\($0.statusLabel)" }.joined(separator: " ") ?? "-"))")
        _ = await wait(240) { approvePending(claude.id); return idle(claude.id) }
        model.refreshSubagents(claude.id)
        try? await Task.sleep(for: .seconds(2))
        check(model.subagents[claude.id]?.contains { $0.state == .done } == true, "subagents claude: finished")
        noError("subagents")
    }

    private static func manage(model: AppModel, check: (Bool, String) -> Void,
                               shot: (String) async -> Void) async {
        func wait(_ seconds: Double, _ condition: () -> Bool) async -> Bool {
            let end = Date.now.addingTimeInterval(seconds)
            while Date.now < end {
                if condition() { return true }
                try? await Task.sleep(for: .milliseconds(500))
            }
            return condition()
        }
        guard let claude = model.chats.first(where: { $0.backend == .claude && $0.title.contains("features e2e") }),
              let codex = model.chats.first(where: { $0.backend == .codex && $0.title.contains("features e2e") }) else {
            check(false, "manage: fixture chats exist")
            return
        }
        let stamp = Int(Date.now.timeIntervalSince1970) % 100000
        func idle(_ id: String) -> Bool { model.chat(id)?.state == .idle }
        func noError(_ label: String) {
            check(model.errorMessage == nil, "\(label): no error (\(model.errorMessage ?? "-"))")
            model.errorMessage = nil
        }
        func agentText(_ id: String, _ marker: String) -> Bool {
            model.timeline(id).contains { item in
                if case .message(_, let author, let text, _, _, _, _) = item, author.kind == .agent, text.contains(marker) {
                    return true
                }
                return false
            }
        }
        func approvePending(_ id: String) {
            if let approval = model.timeline(id).pendingApproval { model.decide(approval.id, .allowed, in: id) }
        }

        // Older history: with HYADEX_HISTORY_PAGE=10 the chat opens on a short page; page back to its start.
        model.select(.chat(claude.id))
        check(await wait(20) { !model.timeline(claude.id).isEmpty }, "history: chat opened with a short page")
        check(model.olderHistory.contains(claude.id), "history: older events offered")
        let shown = model.timeline(claude.id).count
        model.loadOlder(claude.id)
        check(await wait(20) { model.timeline(claude.id).count > shown && !model.loadingOlder.contains(claude.id) },
              "history: older page prepended (\(shown) → \(model.timeline(claude.id).count) items)")
        await shot("manage-1-history")
        var pages = 0
        while model.olderHistory.contains(claude.id), pages < 300 {
            model.loadOlder(claude.id)
            _ = await wait(20) { !model.loadingOlder.contains(claude.id) }
            pages += 1
        }
        check(!model.olderHistory.contains(claude.id), "history: reached the first event after \(pages + 1) page(s)")
        noError("history")

        // Server folders.
        let folders = await model.directorySuggestions("/tmp/hyadex-features-e2e/")
        check(folders.message == nil, "folders: the server lists a folder's subfolders (\(folders.folders.count))")
        let home = await model.directorySuggestions("~")
        check(!home.folders.isEmpty && home.folders.allSatisfy { $0.path.hasPrefix("/") },
              "folders: ~ expands on the server (\(home.folders.count) folders)")
        let prefix = await model.directorySuggestions("/tmp/hyadex-feat")
        check(prefix.folders.first?.name == "hyadex-features-e2e", "folders: a prefix completes")

        // Attachments: upload a file and ask about it.
        let marker = "attach-\(stamp)"
        let file = URL(fileURLWithPath: NSTemporaryDirectory()).appending(path: "note-\(stamp).txt")
        try? "The secret word is \(marker).\n".write(to: file, atomically: true, encoding: .utf8)
        model.select(.chat(codex.id))
        model.attach([file], toChat: codex.id)
        check(await wait(30) { model.attachments[codex.id]?.first?.fileID != nil }, "attachments: uploaded")
        model.send("What is the secret word in the attached file? Reply with just the word.", toChat: codex.id)
        check(model.attachments[codex.id] == nil, "attachments: composer cleared after send")
        check(await wait(240) { agentText(codex.id, marker) && idle(codex.id) }, "attachments: agent read the file")
        check(model.timeline(codex.id).contains { if case .media(_, let author, _) = $0 { author.kind == .user } else { false } },
              "attachments: shown in the conversation")
        await shot("manage-2-attachment")
        noError("attachments")

        // Start from an empty queue (earlier runs may have left held messages).
        for message in model.queues[codex.id] ?? [] { model.withdrawQueued(message.id, in: codex.id) }
        _ = await wait(20) { (model.queues[codex.id] ?? []).isEmpty }
        // Queue: two messages wait behind a long answer; reorder, then Force Send.
        model.send("Write the integers from 1 to 3000, one per line, then the word q0-\(stamp).", toChat: codex.id)
        check(await wait(60) { model.chat(codex.id)?.state == .running }, "queue: long answer running")
        model.send("Reply with exactly q1-\(stamp).", toChat: codex.id)
        model.send("Reply with exactly q2-\(stamp).", toChat: codex.id)
        check(await wait(30) { (model.queues[codex.id]?.count ?? 0) == 2 }, "queue: two messages queued")
        check(model.queues[codex.id]?.first?.text.contains("q1-") == true, "queue: kept the order they were sent in")
        if let second = model.queues[codex.id]?.last {
            model.moveQueued(second.id, up: true, in: codex.id)
            check(await wait(20) { model.queues[codex.id]?.first?.text.contains("q2-") == true },
                  "queue: moved q2 to the front (server order)")
        }
        await shot("manage-3-queue")
        if let first = model.queues[codex.id]?.first {
            check(model.chat(codex.id)?.state == .running, "queue: long answer still running at Force Send")
            model.runQueuedNow(first.id, in: codex.id)
            check(await wait(60) { !(model.queues[codex.id]?.contains { $0.id == first.id } ?? false) },
                  "queue: Force Send took q2")
        }
        check(await wait(300) { agentText(codex.id, "q2-\(stamp)") && agentText(codex.id, "q1-\(stamp)") && idle(codex.id) },
              "queue: q2 and then q1 answered")
        noError("queue")

        // Stop holds the queue; a new message still runs at once; 继续发送 sends the held one.
        model.send("Write the integers from 1 to 3000, one per line, then the word h0-\(stamp).", toChat: codex.id)
        check(await wait(60) { model.chat(codex.id)?.state == .running }, "hold: long answer running")
        model.send("Reply with exactly h1-\(stamp).", toChat: codex.id)
        check(await wait(30) { (model.queues[codex.id]?.count ?? 0) == 1 }, "hold: one message queued")
        model.stop(codex.id)
        check(await wait(60) { model.queues[codex.id]?.first?.hold == .stopped && idle(codex.id) },
              "hold: Stop kept the queued message, held")
        await shot("manage-3b-held")
        model.send("Reply with exactly h2-\(stamp).", toChat: codex.id)
        check(await wait(180) { agentText(codex.id, "h2-\(stamp)") && idle(codex.id) },
              "hold: a new message runs at once past the held one")
        check(model.queues[codex.id]?.first?.hold == .stopped && !agentText(codex.id, "h1-\(stamp)"),
              "hold: the held message stayed held")
        model.resumeQueue(in: codex.id)
        check(await wait(180) { agentText(codex.id, "h1-\(stamp)") && (model.queues[codex.id] ?? []).isEmpty && idle(codex.id) },
              "hold: 继续发送 sent the held message")
        noError("hold")

        // Compaction: Codex natively, Claude with /compact.
        _ = await wait(30) { idle(codex.id) }
        check(model.canCompact(codex.id), "compact codex: offered")
        model.compact(codex.id)
        check(await wait(240) {
            model.timeline(codex.id).contains { if case .divider(_, "上下文已压缩") = $0 { true } else { false } }
        }, "compact codex: compaction completed")
        noError("compact codex")
        model.select(.chat(claude.id))
        model.loadCommandsIfNeeded(forChat: claude.id)
        _ = await wait(60) { model.commands[claude.id]?.available == true }
        _ = await wait(30) { idle(claude.id) }
        let before = model.contextUsage[claude.id]?.tokens ?? 0
        check(model.canCompact(claude.id), "compact claude: /compact offered")
        model.compact(claude.id)
        _ = await wait(20) { model.chat(claude.id)?.state == .running }
        check(await wait(240) { idle(claude.id) }, "compact claude: /compact finished")
        _ = await wait(20) { (model.contextUsage[claude.id]?.tokens ?? before) != before }
        print("[e2e] compact claude: context \(before) → \(model.contextUsage[claude.id]?.tokens ?? -1) tokens")
        await shot("manage-4-compact")
        noError("compact claude")

        // Emergency alert raised by the agent through its own helper.
        let drill = "e2e drill \(stamp)"
        model.send("This is a test drill requested by the user. Use your Hyadex emergency alert helper to raise one "
                   + "emergency alert with exactly this message: \(drill). Then reply done.", toChat: claude.id)
        let raised = await wait(240) {
            approvePending(claude.id)
            return model.emergencies.contains { $0.message.contains(drill) }
        }
        check(raised, "emergency: alert arrived over the emergency stream")
        await shot("manage-5-emergency")
        if let alert = model.emergencies.first(where: { $0.message.contains(drill) }) {
            model.acknowledge(alert)
            try? await Task.sleep(for: .seconds(3))
            check(!model.emergencies.contains { $0.alertID == alert.alertID }, "emergency: acknowledged and cleared")
        }
        _ = await wait(120) { approvePending(claude.id); return idle(claude.id) }
        noError("emergency")

        // Jobs: a chat job and a group job, edited then deleted.
        model.createJob(title: "e2e job \(stamp)", prompt: "Reply ok.", target: .chat(claude.id),
                        schedule: .cron("0 0 9 * * 1-5"), enabled: false)
        check(await wait(20) { model.jobs.contains { $0.title == "e2e job \(stamp)" } }, "jobs: chat job created")
        if let job = model.jobs.first(where: { $0.title == "e2e job \(stamp)" }) {
            check(job.spec == .cron("0 0 9 * * 1-5") && !job.enabled, "jobs: schedule and disabled state kept")
            model.updateJob(job.id, title: "e2e job \(stamp) edited", prompt: "Reply ok.",
                            schedule: .interval(seconds: 7200, repeats: true, firstRun: nil), enabled: false)
            check(await wait(20) { model.job(job.id)?.title.hasSuffix("edited") == true && model.job(job.id)?.schedule == "每 2 小时" },
                  "jobs: edited to every 2 hours")
            model.select(.jobs)
            await shot("manage-6-jobs")
            model.deleteJob(job.id)
            try? await Task.sleep(for: .seconds(5))
            check(model.job(job.id) == nil, "jobs: chat job deleted")
        }
        noError("jobs")

        // Rename, archive and delete: a fresh chat and a fresh group (no turns run).
        let knownChats = Set(model.chats.map(\.id))
        _ = model.createChat(backend: .claude, model: "", cwd: "/tmp/hyadex-features-e2e", permission: "Manual")
        check(await wait(20) { model.chats.contains { !knownChats.contains($0.id) } }, "chats: scratch chat created")
        if let scratch = model.chats.first(where: { !knownChats.contains($0.id) }) {
            model.rename(.chat(scratch.id), to: "e2e scratch \(stamp)")
            try? await Task.sleep(for: .seconds(5))
            check(model.chat(scratch.id)?.title == "e2e scratch \(stamp)", "chats: renamed (server list)")
            model.setArchived(.chat(scratch.id), true)
            try? await Task.sleep(for: .seconds(5))
            check(model.archivedChats.contains { $0.id == scratch.id }, "chats: archived (server list)")
            model.select(.chat(claude.id))
            await shot("manage-7-archived")
            model.setArchived(.chat(scratch.id), false)
            try? await Task.sleep(for: .seconds(5))
            check(model.chat(scratch.id)?.archived == false, "chats: unarchived")
            model.delete(.chat(scratch.id))
            try? await Task.sleep(for: .seconds(6))
            check(model.chat(scratch.id) == nil, "chats: deleted (gone from the server list)")
        }
        let knownGroups = Set(model.groups.map(\.id))
        model.createGroup(title: "e2e scratch group \(stamp)", cwd: "/tmp/hyadex-features-e2e",
                          participants: [Participant(handle: "claude", backend: .claude, role: "", model: "")])
        check(await wait(20) { model.groups.contains { !knownGroups.contains($0.id) } }, "groups: scratch group created")
        if let group = model.groups.first(where: { !knownGroups.contains($0.id) }) {
            model.createJob(title: "e2e group job \(stamp)", prompt: "Reply ok.", target: .group(group.id),
                            schedule: .cron("0 0 9 * * *"), enabled: false)
            check(await wait(20) { model.jobs.contains { $0.groupID == group.id } }, "jobs: group job created")
            if let job = model.jobs.first(where: { $0.groupID == group.id }) {
                model.deleteJob(job.id)
                try? await Task.sleep(for: .seconds(5))
                check(model.job(job.id) == nil, "jobs: group job deleted through the group route")
            }
            model.rename(.group(group.id), to: "e2e group renamed \(stamp)")
            try? await Task.sleep(for: .seconds(5))
            check(model.group(group.id)?.title == "e2e group renamed \(stamp)", "groups: renamed")
            model.setArchived(.group(group.id), true)
            try? await Task.sleep(for: .seconds(5))
            check(model.archivedGroups.contains { $0.id == group.id }, "groups: archived")
            model.delete(.group(group.id))
            try? await Task.sleep(for: .seconds(6))
            check(model.group(group.id) == nil, "groups: deleted")
        }
        noError("rename/archive/delete")
    }

    private static func capabilities(model: AppModel, check: (Bool, String) -> Void,
                                     shot: (String) async -> Void) async {
        func wait(_ seconds: Double, _ condition: () -> Bool) async -> Bool {
            let end = Date.now.addingTimeInterval(seconds)
            while Date.now < end {
                if condition() { return true }
                try? await Task.sleep(for: .milliseconds(500))
            }
            return condition()
        }
        guard let claude = model.chats.first(where: { $0.backend == .claude && $0.title.contains("features e2e") }),
              let codex = model.chats.first(where: { $0.backend == .codex && $0.title.contains("features e2e") }) else {
            check(false, "capabilities: fixture chats exist")
            return
        }
        func idle(_ id: String) -> Bool { model.chat(id)?.state == .idle || model.chat(id) == nil }
        func noError(_ label: String) {
            check(model.errorMessage == nil, "\(label): no error (\(model.errorMessage ?? "-"))")
            model.errorMessage = nil
        }
        func agentText(_ id: String, containing marker: String) -> Bool {
            model.timeline(id).contains { item in
                if case .message(_, let author, let text, _, _, _, _) = item, author.kind == .agent, text.contains(marker) {
                    return true
                }
                return false
            }
        }

        // MCP servers, as each agent loaded them.
        for chat in [claude, codex] {
            model.select(.chat(chat.id))
            model.refreshMCP(forChat: chat.id)
            check(await wait(90) { model.mcp[chat.id] != nil && !model.mcpBusy.contains(chat.id) },
                  "mcp \(chat.backend.rawValue): status loaded")
            let status = model.mcp[chat.id]
            check(status?.available == true && !(status?.servers.isEmpty ?? true),
                  "mcp \(chat.backend.rawValue): \(status?.servers.count ?? 0) server(s): "
                  + (status?.servers.map { "\($0.name)=\($0.status.rawValue)" }.joined(separator: " ") ?? "-"))
        }
        model.select(.chat(claude.id))
        if let server = model.mcp[claude.id]?.servers.first(where: { $0.enabled && $0.status != .needsAuth }) {
            model.controlMCP("disable", server: server.name, forChat: claude.id)
            check(await wait(60) { model.mcp[claude.id]?.servers.first { $0.name == server.name }?.enabled == false },
                  "mcp claude: disabled \(server.name)")
            await shot("capabilities-1-mcp-disabled")
            model.controlMCP("enable", server: server.name, forChat: claude.id)
            check(await wait(60) { model.mcp[claude.id]?.servers.first { $0.name == server.name }?.enabled == true },
                  "mcp claude: enabled \(server.name) again")
            model.controlMCP("reconnect", server: server.name, forChat: claude.id)
            _ = await wait(90) { !model.mcpBusy.contains(claude.id) }
            noError("mcp claude: reconnect \(server.name)")
            check(model.mcp[claude.id]?.servers.first { $0.name == server.name }?.status == .connected,
                  "mcp claude: \(server.name) connected after reconnect")
        }
        await shot("capabilities-2-mcp")

        // Slash commands and skills.
        for chat in [claude, codex] {
            model.loadCommandsIfNeeded(forChat: chat.id)
            check(await wait(90) { model.commands[chat.id]?.available == true },
                  "commands \(chat.backend.rawValue): \(model.commands[chat.id]?.commands.count ?? 0) listed")
        }
        check(model.slashSuggestions(forChat: claude.id, partial: "cont").contains { $0.invocation == "/context" },
              "commands claude: /cont suggests /context")
        check(model.slashSuggestions(forChat: codex.id, partial: "goal").first?.invocation == "/goal",
              "commands codex: /goal offered")
        model.drafts[claude.id] = "/cont"
        await shot("capabilities-3-slash")
        model.drafts[claude.id] = ""
        let before = model.timeline(claude.id).count
        model.send("/context", toChat: claude.id)
        check(await wait(30) { model.timeline(claude.id).count > before }, "commands claude: /context accepted")
        check(await wait(180) { idle(claude.id) }, "commands claude: /context finished")
        noError("commands claude: /context")
        let skillMarker = "e2e-skill-\(Int(Date.now.timeIntervalSince1970) % 100000)"
        model.select(.chat(codex.id))
        model.send("/api-design-principles In one line, name the HTTP verb for creating a resource, then write \(skillMarker).",
                   toChat: codex.id)
        check(await wait(240) { agentText(codex.id, containing: skillMarker) && idle(codex.id) },
              "skills codex: skill turn answered")
        noError("skills codex")
        await shot("capabilities-4-skill")

        // Claude Code /goal: a condition it can meet, then one it cannot (cleared while active).
        model.select(.chat(claude.id))
        check(await wait(30) { model.goalSupport[claude.id]?.available == true }, "goal claude: offered")
        model.send("/goal The file hello.txt in the working folder contains the word hello. Only read it; change nothing.",
                   toChat: claude.id)
        check(await wait(30) { model.goals[claude.id] != nil }, "goal claude: set (\(model.goals[claude.id]?.statusLabel ?? "-"))")
        await shot("capabilities-5-claude-goal")
        check(await wait(300) { model.goals[claude.id]?.status == .achieved && idle(claude.id) },
              "goal claude: achieved (\(model.goals[claude.id]?.statusLabel ?? "-"), \(model.goals[claude.id]?.iterations ?? 0) check(s))")
        noError("goal claude")
        await shot("capabilities-6-claude-achieved")
        model.setGoal("The user has typed the word banana in this chat.", forChat: claude.id)
        check(await wait(60) { model.goals[claude.id]?.status == .active }, "goal claude: unmeetable goal active")
        model.clearGoal(forChat: claude.id)
        check(await wait(120) { model.goals[claude.id].map { !$0.isOpen } ?? true }, "goal claude: cleared (\(model.goals[claude.id]?.statusLabel ?? "none"))")
        _ = await wait(120) { idle(claude.id) }
        noError("goal claude: clear")

        // Codex persistent goal with budgets, then pause and clear.
        model.select(.chat(codex.id))
        check(await wait(30) { model.goalSupport[codex.id]?.available == true }, "goal codex: offered")
        let goalMarker = "e2e-goal-\(Int(Date.now.timeIntervalSince1970) % 100000)"
        model.setGoal("Reply with exactly \(goalMarker), then mark this goal complete.", tokenBudget: 400_000,
                      timeLimitMinutes: 10, forChat: codex.id)
        check(await wait(60) { model.goals[codex.id] != nil }, "goal codex: set")
        check(model.goals[codex.id]?.tokenBudget == 400_000 && model.goals[codex.id]?.timeBudgetSeconds == 600,
              "goal codex: budgets kept")
        await shot("capabilities-7-codex-goal")
        check(await wait(300) { model.goals[codex.id]?.status == .complete && idle(codex.id) },
              "goal codex: complete (\(model.goals[codex.id]?.statusLabel ?? "-"))")
        check(agentText(codex.id, containing: goalMarker), "goal codex: worked toward the objective")
        noError("goal codex")
        model.setGoal("List every prime number below 10^12, one per message.", forChat: codex.id)
        check(await wait(60) { model.goals[codex.id]?.status == .active }, "goal codex: second goal active")
        model.pauseGoal(forChat: codex.id)
        check(await wait(60) { model.goals[codex.id]?.status == .paused }, "goal codex: paused")
        _ = await wait(120) { idle(codex.id) }
        await shot("capabilities-8-codex-paused")
        model.clearGoal(forChat: codex.id)
        check(await wait(60) { model.goals[codex.id] == nil }, "goal codex: cleared")
        noError("goal codex: pause and clear")

        // A group goal set on one member.
        let known = Set(model.groups.map(\.id))
        model.createGroup(title: "features e2e group", cwd: "/tmp/hyadex-features-e2e",
                          participants: [Participant(handle: "claude", backend: .claude, role: "", model: "")])
        check(await wait(30) { model.groups.contains { !known.contains($0.id) } }, "group goal: group created")
        guard let group = model.groups.first(where: { !known.contains($0.id) }) else { return }
        model.select(.group(group.id))
        _ = await wait(15) { model.group(group.id)?.participants.first?.sessionID != nil }
        model.setGroupGoal("The user has typed the word banana in this group.", handle: "claude", inGroup: group.id)
        check(await wait(60) { model.group(group.id)?.goal?.handle == "claude" }, "group goal: set on @claude")
        check(await wait(60) { model.goal(forGroup: group.id)?.state?.status == .active },
              "group goal: member goal active (\(model.goal(forGroup: group.id)?.state?.statusLabel ?? "-"))")
        await shot("capabilities-9-group-goal")
        model.clearGroupGoal(inGroup: group.id)
        check(await wait(120) { model.goal(forGroup: group.id)?.state.map { !$0.isOpen } ?? false },
              "group goal: cleared on the member")
        noError("group goal")
    }
}
