import Foundation
import Testing
@testable import HyadexCore

struct MentionParserTests {
    @Test func findsKnownHandlesOutsideCode() {
        let handles: Set<String> = ["claude", "codex"]
        #expect(MentionParser.mentions(in: "@claude 看下 `@codex` 的写法", handles: handles) == ["claude"])
        #expect(MentionParser.mentions(in: "@Codex 和 @claude，再 @codex", handles: handles) == ["codex", "claude"])
        #expect(MentionParser.mentions(in: "mail@codex.dev", handles: handles) == [])
        #expect(MentionParser.mentions(in: "@all 都说说", handles: handles) == ["all"])
        #expect(MentionParser.mentions(in: "@nobody", handles: handles) == [])
    }

    @Test func detectsPartialMentionBeingTyped() {
        #expect(MentionParser.partialMention(at: "hi @co") == "co")
        #expect(MentionParser.partialMention(at: "@") == "")
        #expect(MentionParser.partialMention(at: "hi @codex done") == nil)
        #expect(MentionParser.partialMention(at: "a@b") == nil)
    }
}

struct TimelineTests {
    @Test func mergesChangesByPathWithLatestDiff() {
        let items: [TimelineItem] = [
            .changes(id: "1", set: FileChangeSet(files: [FileChange(path: "a", added: 1, removed: 0, diff: "old"),
                                                        FileChange(path: "b", added: 2, removed: 1, diff: "b")])),
            .message(id: "2", author: .agent("codex", .codex), text: "", time: .now,
                     receipt: FileChangeSet(files: [FileChange(path: "a", added: 3, removed: 1, diff: "new")])),
        ]
        let merged = items.allChanges
        #expect(merged.files.map(\.path) == ["a", "b"])
        #expect(merged.files[0].diff == "new")
        #expect(merged.added == 5)
    }

    @Test func groupStateIsTheMostUrgentMemberState() {
        var group = SampleData.groups(now: .now)[0]
        group.participants[0].state = .running
        group.participants[1].state = .needsAction
        #expect(group.state == .needsAction)
        group.participants[1].state = .idle
        #expect(group.state == .running)
    }
}

@MainActor
struct AppModelTests {
    @Test func messagesToABusyChatAreQueuedAndEditable() {
        let model = AppModel()
        model.send("第一条", toChat: "c3")   // c3 is running in the sample data
        #expect(model.queues["c3"]?.map(\.text).contains("第一条") == true)
        let id = model.queues["c3"]!.last!.id
        model.editQueued(id, in: "c3")
        #expect(model.drafts["c3"] == "第一条")
        #expect(model.queues["c3"]?.contains { $0.id == id } == false)
    }

    @Test func approvingAPlanStartsTheFirstTask() {
        let model = AppModel()
        model.approve(run: "r2")
        let run = model.run("r2")!
        #expect(run.status == .executing)
        #expect(run.tasks[0].state == .executing)
    }
}
