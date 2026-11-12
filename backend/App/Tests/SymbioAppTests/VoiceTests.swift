import Foundation
import SymbioCore
import Testing
@testable import SymbioApp

/// tests/test_voice_input.py: only a fresh temp clip is accepted, transcribed, deleted and sent back.

private func clip(_ name: String? = nil, _ data: Data = Data("x".utf8)) -> URL {
    let url = URL(fileURLWithPath: tempDirectory())
        .appendingPathComponent(name ?? "\(clipPrefix)test-\(UUID().uuidString).m4a")
    FileManager.default.createFile(atPath: url.path, contents: data)
    return url
}

private func exists(_ url: URL) -> Bool { FileManager.default.fileExists(atPath: url.path) }

private func voice(_ key: String?, _ result: Result<String, any Error>) -> (NativeBridge, FakeWorker, Events) {
    let (bridge, worker, events) = makeBridge(voiceKey: key, transcriber: { _, _ in try result.get() })
    events.clear()
    return (bridge, worker, events)
}

@Test func clipPathRejectsForeignNamesLocationsSymlinksAndEmptyOrHugeFiles() throws {
    let good = clip()
    #expect(clipPath(.string(good.path)) == good)
    #expect(clipPath(nil) == nil)
    #expect(clipPath(.string("/tmp/a\0b")) == nil)
    let other = try temporaryFolder().appendingPathComponent("\(clipPrefix)x.m4a")
    FileManager.default.createFile(atPath: other.path, contents: Data("x".utf8))
    #expect(clipPath(.string(other.path)) == nil)  // not in the temp directory root
    let foreign = clip("notmine-\(UUID().uuidString).m4a")
    #expect(clipPath(.string(foreign.path)) == nil)
    let empty = clip("\(clipPrefix)empty-\(UUID().uuidString).m4a", Data())
    #expect(clipPath(.string(empty.path)) == nil)
    let link = URL(fileURLWithPath: tempDirectory()).appendingPathComponent("\(clipPrefix)link-\(UUID().uuidString).m4a")
    try FileManager.default.createSymbolicLink(atPath: link.path, withDestinationPath: good.path)
    #expect(clipPath(.string(link.path)) == nil)
    for url in [good, link, foreign, empty] { unlink(url.path) }
}

@Test func voiceCommandTranscribesDeletesTheClipAndReportsText() async {
    let (bridge, _, events) = voice("k", .success("고양이 사진 찾아줘"))
    let path = clip()
    #expect(bridge.command(["action": "voice", "path": .string(path.path)]))
    await bridge.voiceTask?.value
    #expect(events.last == ["type": "transcript", "text": "고양이 사진 찾아줘", "error": nil])
    #expect(!exists(path) && !bridge.transcribing)
}

@Test func voiceCommandReportsMissingKeyAndFailureWithoutSubmitting() async {
    for (key, result, expected) in [(nil, Result<String, any Error>.success("x"), "GROQ_API_KEY"),
                                    ("k", .failure(Boom(message: "boom")), "Boom")] {
        let (bridge, worker, events) = voice(key, result)
        let path = clip()
        _ = bridge.command(["action": "voice", "path": .string(path.path)])
        await bridge.voiceTask?.value
        #expect(events.last["error"]?.stringValue?.contains(expected) == true && events.last["text"] == "")
        #expect(!exists(path))
        #expect(worker.count("submit") == 0)
    }
}

@Test func voiceCommandShowsNothingForEmptyOrStockPhraseText() async {
    for result in ["", "감사합니다.", "시청해 주셔서 감사합니다"] {
        let (bridge, worker, events) = voice("k", .success(result))
        _ = bridge.command(["action": "voice", "path": .string(clip().path)])
        await bridge.voiceTask?.value
        #expect(events.last == ["type": "transcript", "text": "", "error": nil])
        #expect(worker.count("submit") == 0)
    }
}

@Test func transcribeRefusesAClipSwappedForASymlinkAfterValidation() async throws {
    let secret = try temporaryFolder().appendingPathComponent("secret.txt")
    try Data("private".utf8).write(to: secret)
    let link = URL(fileURLWithPath: tempDirectory()).appendingPathComponent("\(clipPrefix)swap-\(UUID().uuidString).m4a")
    try FileManager.default.createSymbolicLink(atPath: link.path, withDestinationPath: secret.path)
    let sent = Box(false)
    await #expect(throws: POSIXError.self) {
        _ = try await transcribe(link, apiKey: "k", transport: { _ in
            sent.value = true
            return (Data(), URLResponse())
        })
    }
    #expect(!sent.value)
    #expect(exists(secret) && !exists(link))
}

@Test func transcribeSendsAMultipartUploadAndStripsTheText() async throws {
    let path = clip()
    let seen = Box<URLRequest?>(nil)
    let text = try await transcribe(path, apiKey: "k", transport: { request in
        seen.value = request
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        return (Data(" 사진 찾아줘\n".utf8), response)
    })
    #expect(text == "사진 찾아줘")
    #expect(!exists(path))
    let request = try #require(seen.value)
    #expect(request.url?.absoluteString == "https://api.groq.com/openai/v1/audio/transcriptions")
    let body = String(decoding: request.httpBody ?? Data(), as: UTF8.self)
    #expect(body.contains("whisper-large-v3-turbo") && body.contains("name=\"language\"\r\n\r\nko"))
}

@Test func cancelDuringTranscriptionDiscardsTheText() async {
    let holder = Box<NativeBridge?>(nil)
    let (bridge, _, events) = makeBridge(voiceKey: "k", transcriber: { _, _ in
        _ = holder.value?.command(["action": "cancel"])
        return "지워줘"
    })
    holder.value = bridge
    _ = bridge.command(["action": "voice", "path": .string(clip().path)])
    await bridge.voiceTask?.value
    #expect(events.last["text"] == "" && events.last["error"]?.stringValue?.contains("취소") == true)
}

@Test func voiceCommandIsRefusedWhenBusyOrTheClipIsNotOurs() {
    let (bridge, _, events) = makeBridge(voiceKey: "k")
    bridge.state = .waitingApproval
    let path = clip()
    _ = bridge.command(["action": "voice", "path": .string(path.path)])
    #expect(events.last["type"] == "rejected" && !exists(path))
    bridge.state = .idle
    _ = bridge.command(["action": "voice", "path": "/etc/passwd"])
    #expect(events.last["type"] == "rejected")
    #expect(FileManager.default.fileExists(atPath: "/etc/passwd"))
}

@Test func purgeStaleRemovesOnlyOldVoiceClips() throws {
    let old = clip(), fresh = clip()
    try FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSince1970: 1)], ofItemAtPath: old.path)
    purgeStale()
    #expect(!exists(old) && exists(fresh))
    unlink(fresh.path)
}
