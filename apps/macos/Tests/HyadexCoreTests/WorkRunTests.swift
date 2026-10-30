import Foundation
import HyadexAPI
import Testing
@testable import HyadexCore

/// The "已处理" rows of a chat turn: their durations, images inside the work, and repeated thoughts.
struct WorkRunTests {
    static func at(_ second: Int) -> JSON { .string(String(format: "2030-01-01T00:%02d:%02dZ", second / 60, second % 60)) }

    @Test func eachRunOfWorkHasItsOwnDuration() {
        var projection = SessionProjection(backend: .claude, cwd: "/w")
        projection.apply([
            ["seq": 1, "type": "turn_started", "id": "u1", "prompt": "Draw a flowerbed", "ts": Self.at(0)],
            ["seq": 2, "type": "reasoning_summary", "id": "t1", "text": "Plan the picture.", "ts": Self.at(14)],
            ["seq": 3, "type": "tool_started", "tool": ["id": "b1", "name": "Bash", "input": ["command": "python draw.py"]],
             "ts": Self.at(20)],
            ["seq": 4, "type": "tool_finished", "tool_id": "b1", "tool": ["id": "b1", "name": "Bash"], "ts": Self.at(60)],
            ["seq": 5, "type": "artifact_created", "id": "a1",
             "artifact": ["id": "art_1", "title": "flowerbed.png", "content_type": "image/png"], "ts": Self.at(68)],
            ["seq": 6, "type": "reasoning_summary", "id": "t2", "text": "Ready for the next question.", "ts": Self.at(72)],
            ["seq": 7, "type": "assistant_text", "id": "m1", "text": "Here it is.", "ts": Self.at(72)],
            // An older server kept the last thought again, as partial.
            ["seq": 8, "type": "reasoning_summary", "id": "t3", "text": "Ready for the next question.\n\n",
             "partial": true, "ts": Self.at(75)],
            ["seq": 9, "type": "turn_finished", "ts": Self.at(75)],
        ])
        // The image does not split the work: one run, from the turn's start to the answer.
        #expect(projection.workSeconds == ["t1": 72])
        #expect(projection.items.filter { if case .thinking = $0 { true } else { false } }.count == 2)
        #expect(projection.items.last.map { if case .message = $0 { true } else { false } } == true)
        #expect(projection.items.contains { $0.floatsPastWork })
    }

    @Test func workAfterTheAnswerIsTimedOnItsOwn() {
        var projection = SessionProjection(backend: .codex, cwd: "/w")
        projection.apply([
            ["seq": 1, "type": "turn_started", "id": "u1", "prompt": "Fix it", "ts": Self.at(0)],
            ["seq": 2, "type": "reasoning_summary", "id": "t1", "text": "Look first.", "ts": Self.at(5)],
            ["seq": 3, "type": "assistant_text", "id": "m1", "text": "Checking the tests now.", "ts": Self.at(10)],
            ["seq": 4, "type": "tool_started", "tool": ["id": "c1", "name": "shell", "input": ["command": "make test"]],
             "ts": Self.at(12)],
            ["seq": 5, "type": "tool_finished", "tool_id": "c1", "tool": ["id": "c1", "name": "shell"], "ts": Self.at(40)],
            ["seq": 6, "type": "turn_finished", "ts": Self.at(41)],
        ])
        #expect(projection.workSeconds == ["t1": 10, "c1": 29])
    }

    /// Codex records a turn the user stopped as finished with `stopped` (the server's real event).
    @Test func aTurnTheUserStoppedReadsAsStopped() {
        var projection = SessionProjection(backend: .codex, cwd: "/w")
        projection.apply([
            ["seq": 1, "type": "turn_started", "id": "u1", "prompt": "Run sleep 60", "ts": Self.at(0)],
            ["seq": 2, "type": "tool_started", "tool": ["id": "c1", "name": "Bash", "input": ["command": "sleep 60"]],
             "ts": Self.at(2)],
            ["seq": 3, "type": "turn_finished", "id": "f1", "exit_code": .null, "result_text": "", "stopped": true,
             "ts": Self.at(12)],
            // The same stop recorded again shows once.
            ["seq": 4, "type": "turn_stopped", "id": "s1", "ts": Self.at(12)],
        ])
        #expect(projection.running == false)
        #expect(projection.items.contains { if case .tool(_, _, _, _, .failed, _) = $0 { true } else { false } })
        #expect(projection.items.filter { if case .divider(_, "已停止") = $0 { true } else { false } }.count == 1)
        #expect(projection.items.last.map { if case .divider(_, "已停止") = $0 { true } else { false } } == true)
    }

    @Test func codexImageGenerationHasAName() {
        #expect(SessionProjection.describe(tool: ["name": "ImageGeneration", "input": [:]], cwd: "/w").title == "生成图片")
    }
}
