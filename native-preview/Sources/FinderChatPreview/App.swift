import AppKit
import SwiftUI

@main
struct FinderChatPreviewApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    var body: some Scene {
        Window("파일 도우미", id: "assistant") {
            ChatPanel().frame(minWidth: 360, minHeight: 480)
        }
        .defaultSize(width: 360, height: 560)
        .windowToolbarStyle(.unifiedCompact)
    }
}

private struct ChatPanel: View {
    @StateObject private var backend = Backend.shared
    @State private var input = ""

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "folder.fill").foregroundStyle(.secondary)
                Text(backend.folders.map { URL(fileURLWithPath: $0).lastPathComponent }.joined(separator: ", "))
                    .font(.callout).lineLimit(2)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 16).padding(.vertical, 10).background(.bar)
            .help(backend.folders.joined(separator: "\n"))
            Divider()
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        status
                        if backend.messages.isEmpty {
                            Text("파일을 찾거나 요약해 보세요.")
                                .foregroundStyle(.secondary).font(.callout)
                        }
                        ForEach(backend.messages) { message in
                            messageView(message)
                        }
                        ForEach(Array(backend.choices.enumerated()), id: \.offset) { index, label in
                            Button { backend.submit("\(index + 1)번") } label: {
                                HStack {
                                    Text("\(index + 1)").foregroundStyle(.secondary)
                                    Text(label).font(.callout).multilineTextAlignment(.leading)
                                    Spacer(minLength: 0)
                                }
                                .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                                .background(.quaternary, in: RoundedRectangle(cornerRadius: 10))
                            }
                            .buttonStyle(.plain).disabled(!backend.canSubmit)
                        }
                        if let approval = backend.approval { approvalView(approval) }
                        if backend.ratable { rating }
                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .padding(16).frame(maxWidth: .infinity, alignment: .leading)
                }
                .textSelection(.enabled)
                .onChange(of: backend.messages.count) { _, _ in proxy.scrollTo("bottom", anchor: .bottom) }
                .onChange(of: backend.approval?.approval_id) { _, _ in proxy.scrollTo("bottom", anchor: .bottom) }
            }
            Divider()
            composer
        }
        .background(Color(nsColor: .textBackgroundColor))
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Menu {
                    Button("최근 작업 되돌리기", systemImage: "arrow.uturn.backward") {
                        if backend.send("undo") { backend.pending = true }
                    }.disabled(!backend.idle)
                    Button("연결 다시 시작", systemImage: "arrow.clockwise") { backend.start() }
                        .disabled(backend.online)
                } label: { Image(systemName: "ellipsis") }
            }
        }
        .task { backend.start() }
        .onDrop(of: [.fileURL], isTargeted: nil) { providers in
            guard backend.idle, providers.count == 1 else { return false }
            _ = providers[0].loadObject(ofClass: URL.self) { url, _ in
                guard let url else { return }
                Task { @MainActor in _ = backend.send("attach", fields: ["path": url.path]) }
            }
            return true
        }
    }

    private var status: some View {
        HStack(spacing: 6) {
            Circle().fill(backend.online ? .green : .orange).frame(width: 5, height: 5)
            Text(statusText).font(.caption).foregroundStyle(.secondary)
            Spacer()
            if backend.pending || ["PLANNING", "EXECUTING_TOOL"].contains(backend.state) {
                ProgressView().controlSize(.small)
                Button("취소") { _ = backend.send("cancel") }.controlSize(.small)
            }
        }
    }

    private var statusText: String {
        if !backend.tool.isEmpty { return "작업 중 · \(backend.tool)" }
        switch backend.state {
        case "IDLE": return "준비됨"
        case "WAITING_APPROVAL": return "승인이 필요해요"
        case "WAITING_USER_INPUT": return "답변이 필요해요"
        case "PLANNING", "RESUMING_PLAN": return "요청을 처리하고 있어요"
        case "STOPPED": return "연결 종료 · 메뉴에서 다시 시작하세요"
        default: return "연결 준비 중"
        }
    }

    @ViewBuilder
    private func messageView(_ message: ChatMessage) -> some View {
        if message.role == "user" {
            HStack {
                Spacer(minLength: 32)
                Text(message.text).font(.callout).padding(12)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
            }
        } else {
            Text((try? AttributedString(markdown: message.text,
                options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
                ?? AttributedString(message.text))
                .font(.callout).lineSpacing(4)
                .foregroundStyle(message.role == "error" ? Color.red : Color.primary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// Title, symbol and button text per operation; path operations show source and destination.
    private func approvalStyle(_ operation: String) -> (title: String, symbol: String, button: String) {
        switch operation {
        case "delete": return ("파일 삭제 승인", "trash", "승인하고 삭제")
        case "move": return ("파일 이동 승인", "arrow.right.circle", "승인하고 이동")
        case "copy": return ("파일 복사 승인", "doc.on.doc", "승인하고 복사")
        case "mkdir": return ("폴더 생성 승인", "folder.badge.plus", "승인하고 생성")
        default: return ("파일 변경 승인", "doc.badge.plus", "승인하고 실행")
        }
    }

    private func approvalView(_ approval: Approval) -> some View {
        let style = approvalStyle(approval.operation)
        let isPathOperation = ["move", "copy", "mkdir"].contains(approval.operation)
        return VStack(alignment: .leading, spacing: 10) {
            Label(style.title, systemImage: style.symbol).font(.headline)
            if isPathOperation {
                if let source = approval.targets.first {
                    Text("원본").font(.caption).foregroundStyle(.secondary)
                    Text(source).font(.caption).textSelection(.enabled)
                }
                Text(approval.operation == "mkdir" ? "만들 폴더" : "목적지")
                    .font(.caption).foregroundStyle(.secondary)
                Text(approval.target).font(.caption).textSelection(.enabled)
                Text("기존 경로를 덮어쓰지 않으며 Undo로 되돌릴 수 있습니다.")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text(approval.target).font(.caption).textSelection(.enabled)
                if !approval.targets.isEmpty {
                    Text("대상 \(approval.targets.count)개").font(.callout.weight(.medium))
                    ForEach(Array(approval.targets.enumerated()), id: \.offset) { index, path in
                        Text("\(index + 1). \(path)").font(.caption).textSelection(.enabled)
                    }
                }
                if !approval.preview.isEmpty {
                    Text(approval.preview).font(.callout).textSelection(.enabled)
                    if approval.truncated { Text("미리보기 일부만 표시").font(.caption).foregroundStyle(.secondary) }
                }
                Text("\(approval.characters)자 · \(approval.operation)").font(.caption).foregroundStyle(.secondary)
            }
            HStack {
                Spacer()
                Button("거절") { backend.decide(false) }.buttonStyle(.bordered)
                Button(style.button) { backend.decide(true) }.buttonStyle(.borderedProminent)
            }.disabled(backend.pending)
        }
        .padding(12).background(.quaternary, in: RoundedRectangle(cornerRadius: 10))
    }

    private var rating: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("답변 평가").font(.caption).foregroundStyle(.secondary)
                Spacer()
                ForEach(1...5, id: \.self) { score in
                    Button(String(score)) { _ = backend.send("rate", fields: ["score": score]) }
                        .buttonStyle(.bordered).controlSize(.mini)
                }
            }
            if let score = backend.lowScore {
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())]) {
                    ForEach([("wrong_file", "엉뚱한 파일"), ("missing_content", "내용 부족"),
                             ("wrong_content", "틀린 내용"), ("too_long", "너무 김"),
                             ("too_slow", "너무 느림"), ("other", "기타")], id: \.0) { tag, title in
                        Button(title) { _ = backend.send("rate", fields: ["score": score, "tags": [tag]]) }
                            .controlSize(.small)
                    }
                }
            }
        }.disabled(!backend.idle)
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let path = backend.attachment {
                HStack {
                    Label(URL(fileURLWithPath: path).lastPathComponent, systemImage: "paperclip")
                        .font(.caption).lineLimit(2).help(path)
                    Spacer()
                    Button { _ = backend.send("detach") } label: { Image(systemName: "xmark") }
                        .buttonStyle(.borderless).disabled(!backend.idle)
                }
            }
            HStack(alignment: .bottom, spacing: 8) {
                Button(action: attach) { Image(systemName: "paperclip") }
                    .buttonStyle(.borderless).disabled(!backend.idle).help("파일 첨부")
                TextField(backend.state == "WAITING_USER_INPUT" ? "답변을 입력하세요" : "명령을 입력하세요",
                          text: $input, axis: .vertical)
                    .textFieldStyle(.plain).lineLimit(1...3).onSubmit(submit)
                Button(action: submit) { Image(systemName: "arrow.up") }
                    .buttonStyle(.borderedProminent).buttonBorderShape(.circle)
                    .disabled(!backend.canSubmit || input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .padding(10)
            .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
            .overlay { RoundedRectangle(cornerRadius: 14).strokeBorder(.separator, lineWidth: 0.5) }
            Text("파일 변경은 승인 후 실행됩니다").font(.system(size: 10)).foregroundStyle(.secondary)
        }.padding(12).background(.bar)
    }

    private func submit() {
        guard backend.canSubmit else { return }
        backend.submit(input)
        input = ""
    }

    private func attach() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.begin { response in
            guard response == .OK, let url = panel.url else { return }
            _ = backend.send("attach", fields: ["path": url.path])
        }
    }
}
