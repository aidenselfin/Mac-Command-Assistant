import Foundation
import SymbioCore

/// Python `voice_action/transcribe.py`: push-to-talk clip -> Korean command text (Groq Whisper).

public let groqBaseURL = "https://api.groq.com/openai/v1"
public let whisperModel = "whisper-large-v3-turbo"
public let maxClipBytes = 20 * 1024 * 1024  // Groq rejects larger uploads
public let clipPrefix = "voice-action-voice-"

/// Whisper invents these stock phrases from silence or room noise.
public let hallucinations: Set<String> = [
    "감사합니다", "시청해 주셔서 감사합니다", "구독과 좋아요", "구독과 좋아요 부탁드립니다",
    "다음 영상에서 만나요", "MBC 뉴스 이덕영입니다",
]

public func isHallucination(_ text: String) -> Bool {
    hallucinations.contains(text.trimmingCharacters(in: CharacterSet(charactersIn: " .!?~,"))
        .replacingOccurrences(of: "  ", with: " "))
}

/// Python `tempfile.gettempdir()` resolved.
func tempDirectory() -> String {
    realPath(NSTemporaryDirectory()) ?? NSTemporaryDirectory()
}

/// Return the clip only if it is a plain file the app just wrote into the temp directory.
public func clipPath(_ raw: JSON?) -> URL? {
    guard let raw = raw?.stringValue, !raw.isEmpty, !raw.contains("\0") else { return nil }
    var link = stat()
    if lstat(raw, &link) == 0, (link.st_mode & S_IFMT) == S_IFLNK { return nil }
    guard let real = realPath(raw) else { return nil }  // resolve once: check and use the same location
    let url = URL(fileURLWithPath: real)
    guard url.lastPathComponent.hasPrefix(clipPrefix), url.deletingLastPathComponent().path == tempDirectory()
    else { return nil }
    var info = stat()
    guard stat(real, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG, info.st_size > 0,
          info.st_size <= maxClipBytes
    else { return nil }
    return url
}

public struct TranscribeError: Error, Equatable, CustomStringConvertible {
    public let status: Int
    public var description: String { "Groq 음성 변환 실패 (HTTP \(status))" }
}

public typealias HTTPTransport = @Sendable (URLRequest) async throws -> (Data, URLResponse)

/// Send the clip to Groq and return stripped text; the clip is always deleted.
public func transcribe(
    _ path: URL, apiKey: String, timeout: Double = 60, transport: HTTPTransport? = nil
) async throws -> String {
    defer { unlink(path.path) }
    // O_NOFOLLOW: a symlink swapped in after clipPath() must not leak other files.
    let fd = open(path.path, O_RDONLY | O_NOFOLLOW)
    guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    let audio = FileHandle(fileDescriptor: fd, closeOnDealloc: true).readDataToEndOfFile()

    let boundary = "symbio-\(UUID().uuidString)"
    var body = Data()
    func field(_ name: String, _ value: String) {
        body.append(Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n".utf8))
    }
    field("model", whisperModel)
    field("language", "ko")
    field("response_format", "text")
    body.append(Data((
        "--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(path.lastPathComponent)\"\r\n"
            + "Content-Type: application/octet-stream\r\n\r\n").utf8))
    body.append(audio)
    body.append(Data("\r\n--\(boundary)--\r\n".utf8))

    var request = URLRequest(url: URL(string: groqBaseURL + "/audio/transcriptions")!, timeoutInterval: timeout)
    request.httpMethod = "POST"
    request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
    request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
    request.httpBody = body
    let send = transport ?? { try await URLSession.shared.data(for: $0) }
    let (data, response) = try await send(request)
    let status = (response as? HTTPURLResponse)?.statusCode ?? 0
    guard (200..<300).contains(status) else { throw TranscribeError(status: status) }
    return String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
}

/// Delete clips left behind by an app quit during recording or transcription.
public func purgeStale(maxAge: Double = 3600) {
    let folder = tempDirectory()
    let now = Date().timeIntervalSince1970
    for name in (try? FileManager.default.contentsOfDirectory(atPath: folder)) ?? [] where name.hasPrefix(clipPrefix) {
        let path = folder + "/" + name
        var info = stat()
        guard lstat(path, &info) == 0, (info.st_mode & S_IFMT) != S_IFLNK else { continue }
        if now - Double(info.st_mtimespec.tv_sec) > maxAge { unlink(path) }
    }
}
