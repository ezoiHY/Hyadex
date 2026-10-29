import Foundation
import HyadexAPI
import Testing
@testable import HyadexCore

/// The live status's latest thought, and the sidebar's project groups.
struct ThoughtSummaryTests {
    @Test func codexShowsItsNewestStepHeading() {
        let text = "**Reading the socket code**\n\nI'm looking at reconnects.\n\n**Planning the backoff**\n\nNext, jitter."
        #expect(ThoughtSummary.line(text) == "Planning the backoff")
    }

    @Test func otherwiseTheLastSentenceSoFar() {
        #expect(ThoughtSummary.line("这是一个宽泛的问题。我打算用中文回复，先给一个概述。") == "我打算用中文回复，先给一个概述。")
        // A sentence just begun brings the one before it.
        #expect(ThoughtSummary.line("Version 3.5 of `foo.swift` is fine. Next") == "Version 3.5 of foo.swift is fine. Next")
        #expect(ThoughtSummary.line("  \n ") == nil)
    }

    @Test func latestThoughtStopsAtTheAnswer() {
        let thinking = TimelineItem.thinking(id: "t", seconds: 0, text: "Plan.")
        let tool = TimelineItem.tool(id: "x", kind: .read, title: "a", output: "", status: .succeeded, seconds: 0)
        let answer = TimelineItem.message(id: "m", author: .agent("claude", .claude), text: "Done", time: .now)
        #expect(ThoughtSummary.latest(in: [thinking, tool]) == "Plan.")
        #expect(ThoughtSummary.latest(in: [thinking, answer]) == nil)
    }

    @MainActor @Test func runningChatKeepsItsFinishedThoughtInView() {
        let model = AppModel(snapshotFixturesAt: .now)
        let index = model.chats.firstIndex { $0.id == "c6" }!
        model.chats[index].state = .running
        model.turnStartedAt["c6"] = .now
        model.timelines["c6"] = SampleData.thinkingTimeline(now: .now)
        let activity = model.activity(forChat: "c6")
        #expect(activity?.kind == .thinking)
        #expect(activity?.backend == .claude)
        #expect(activity?.detail?.hasPrefix("我打算用中文回复") == true)
        // A live thought replaces it.
        model.liveReasoning["c6"] = "**Checking the formula**\n\nb² − 4ac"
        #expect(model.activity(forChat: "c6")?.detail == "Checking the formula")
    }
}

struct ProjectGroupingTests {
    private func chat(_ id: String, cwd: String, folder: String? = nil, minutesAgo: Double) -> ChatSummary {
        ChatSummary(id: id, title: id, backend: .claude, model: "", permission: "", cwd: cwd, preview: "",
                    lastActivity: Date(timeIntervalSince1970: 1_000_000 - minutesAgo * 60), folder: folder)
    }

    @Test func chatsGroupByProjectThenDirectory() {
        let sections = ProjectGrouping.sections(chats: [
            chat("a", cwd: "~/Code/Hyadex", minutesAgo: 5),
            chat("b", cwd: "~/Code/Hyadex/", minutesAgo: 1),
            chat("c", cwd: "~/Work/app", folder: "发布", minutesAgo: 30),
            chat("d", cwd: "~/Notes", minutesAgo: 2),
            chat("e", cwd: "~", minutesAgo: 0),
            chat("f", cwd: "~/Other/app", minutesAgo: 40),
        ], projects: [
            ChatProject(name: "笔记", directory: "~/Notes/"),
            ChatProject(name: "空项目"),
        ])
        // Newest first; empty projects after, the home folder last.
        #expect(sections.map(\.title) == ["Hyadex", "笔记", "发布", "app", "空项目", "其他"])
        let hyadex = sections[0]
        #expect(hyadex.kind == .directory && hyadex.chats.map(\.id) == ["b", "a"])
        #expect(sections[1].kind == .project && sections[1].chats.map(\.id) == ["d"])
        #expect(sections.first { $0.title == "发布" }?.chats.map(\.id) == ["c"])
        #expect(sections.last?.kind == .other && sections.last?.chats.map(\.id) == ["e"])
        #expect(sections.first { $0.title == "空项目" }?.chats.isEmpty == true)
    }

    @Test func sameNamedDirectoriesShowTheirParent() {
        let titles = ProjectGrouping.sections(chats: [
            chat("a", cwd: "~/One/app", minutesAgo: 1),
            chat("b", cwd: "~/Two/app", minutesAgo: 2),
        ], projects: []).map(\.title)
        #expect(titles == ["One/app", "Two/app"])
    }

    @Test func serverDefaultFolderIsNoProject() {
        let general = LiveMapping.chat(["id": "s1", "backend": "claude", "folder": "General"], previous: nil)
        let named = LiveMapping.chat(["id": "s2", "backend": "claude", "folder": " 发布 "], previous: nil)
        #expect(general?.folder == nil)
        #expect(named?.folder == "发布")
    }

    @MainActor @Test func renamingAProjectKeepsItsChatsWhileOffline() {
        let model = AppModel(snapshotFixturesAt: .now)
        model.saveProject(ChatProject(name: " 发布 ", directory: "~/Code/Hyadex/"))
        #expect(model.projects == [ChatProject(name: "发布", directory: "~/Code/Hyadex")])
        model.saveProject(ChatProject(name: "General"))
        #expect(model.projects.count == 1)
        // Chats in the directory join the project without being moved.
        #expect(model.projectSections.first { $0.title == "发布" }?.chats.contains { $0.cwd == "~/Code/Hyadex" } == true)
        model.deleteProject("发布")
        #expect(model.projects.isEmpty)
    }
}
