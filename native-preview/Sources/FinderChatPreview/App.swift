import AppKit
import SwiftUI

@main
struct FinderChatPreviewApp: App {
    var body: some Scene {
        WindowGroup("파일 도우미") {
            ChatPanel()
                .frame(minWidth: 360, minHeight: 480)
        }
        .defaultSize(width: 360, height: 560)
        .windowToolbarStyle(.unifiedCompact)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}

private enum Example: String, CaseIterable, Identifiable {
    case conversation = "대화"
    case selection = "파일 선택"
    case approval = "저장 승인"

    var id: String { rawValue }
}

private struct ChatPanel: View {
    @State private var example: Example = .conversation
    @State private var input = ""
    @State private var notice: String?
    @State private var sent: String?

    var body: some View {
        VStack(spacing: 0) {
            folderBar
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    HStack(spacing: 5) {
                        Circle().fill(.green).frame(width: 5, height: 5)
                        Text("무엇을 도와드릴까요?")
                            .font(.caption).foregroundStyle(.secondary)
                        Spacer()
                    }
                    requestBubble
                    answer
                    if let sent {
                        HStack {
                            Spacer(minLength: 32)
                            Text(sent)
                                .padding(10)
                                .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
                        }
                        Text("외형 미리보기입니다. 실제 작업은 실행하지 않아요.")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                    if let notice {
                        Label(notice, systemImage: "info.circle")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .textSelection(.enabled)
            Divider()
            composer
        }
        .background(Color(nsColor: .textBackgroundColor))
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Menu {
                    Button("되돌리기 미리보기", systemImage: "arrow.uturn.backward") {
                        notice = "되돌리기 버튼 미리보기"
                    }
                    Divider()
                    Picker("화면 예시", selection: $example) {
                        ForEach(Example.allCases) { item in
                            Text(item.rawValue).tag(item)
                        }
                    }
                } label: {
                    Image(systemName: "ellipsis")
                }
                .help("화면 예시 선택")
            }
        }
        .onChange(of: example) { _, _ in
            notice = nil
            sent = nil
        }
        .accessibilityIdentifier("chat-panel")
    }

    private var folderBar: some View {
        HStack(spacing: 8) {
            Image(systemName: "folder.fill").foregroundStyle(.secondary)
            Text("작업 폴더").font(.callout)
            Image(systemName: "chevron.right").font(.system(size: 9)).foregroundStyle(.tertiary)
            Text("회의 자료").font(.callout).foregroundStyle(.secondary)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(.bar)
    }

    private var requestBubble: some View {
        HStack {
            Spacer(minLength: 32)
            Text(example == .approval ? "요약을 summary.md에 저장해줘" : "지난 회의록을 요약해줘")
                .font(.callout)
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
        }
    }

    @ViewBuilder
    private var answer: some View {
        switch example {
        case .conversation:
            VStack(alignment: .leading, spacing: 12) {
                Text("회의의 핵심을 정리했어요.")
                    .font(.system(size: 14, weight: .medium))
                Text("출시 일정은 다음 주 금요일로 정했습니다. 김민수님이 테스트 결과를 확인하고, 이지은님이 안내 문서를 준비합니다.")
                    .font(.callout).lineSpacing(4)
                Text("남은 확인 사항")
                    .font(.callout.weight(.medium))
                Text("최종 테스트 결과에 따라 출시 여부를 확정하기로 했어요.")
                    .font(.callout).lineSpacing(4)
                Label("회의록-10월.md", systemImage: "doc.text")
                    .font(.caption).foregroundStyle(.secondary)
                HStack(spacing: 6) {
                    Text("답변 평가").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    ForEach(1...5, id: \.self) { score in
                        Button(String(score)) { notice = "\(score)점 선택 · 미리보기" }
                            .controlSize(.mini)
                            .buttonStyle(.bordered)
                            .help("\(score)점")
                    }
                }
                .padding(.top, 4)
            }
        case .selection:
            VStack(alignment: .leading, spacing: 12) {
                Text("어떤 회의록을 읽을까요?").font(.system(size: 14, weight: .medium))
                Text("두 개의 파일을 찾았어요.").font(.callout).foregroundStyle(.secondary)
                candidate("회의록-10월.md", detail: "회의 자료 · 10월 12일 · 4 KB")
                candidate("회의록-9월.md", detail: "보관 자료 · 9월 28일 · 3 KB")
            }
        case .approval:
            VStack(alignment: .leading, spacing: 12) {
                Label("새 파일 저장", systemImage: "doc.badge.plus")
                    .font(.system(size: 14, weight: .medium))
                Text("내용과 저장 위치를 확인해 주세요.")
                    .font(.callout).foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 8) {
                    Text("summary.md").font(.callout.weight(.medium))
                    Text("회의 자료 / summary.md").font(.caption).foregroundStyle(.secondary)
                    Divider()
                    Text("회의 요약\n\n출시 일정: 다음 주 금요일\n테스트 결과 확인: 김민수\n안내 문서 준비: 이지은")
                        .font(.callout).lineSpacing(3)
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 10))
                HStack {
                    Text("외형 미리보기").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button("거절") { notice = "거절 화면 미리보기 · 파일 변경 없음" }
                        .buttonStyle(.bordered)
                    Button("승인하고 저장") { notice = "승인 화면 미리보기 · 파일 변경 없음" }
                        .buttonStyle(.borderedProminent)
                }
            }
        }
    }

    private func candidate(_ name: String, detail: String) -> some View {
        Button {
            notice = "\(name) 선택 · 미리보기"
        } label: {
            HStack(spacing: 10) {
                Image(systemName: "doc.text").font(.title3).foregroundStyle(.secondary)
                VStack(alignment: .leading, spacing: 4) {
                    Text(name).font(.callout.weight(.medium))
                    Text(detail).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary)
            }
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 10))
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .bottom, spacing: 8) {
                Button { notice = "첨부 버튼 미리보기" } label: {
                    Image(systemName: "paperclip")
                }
                .buttonStyle(.borderless)
                .help("파일 첨부 · 미리보기")
                TextField("명령을 입력하세요", text: $input, axis: .vertical)
                    .textFieldStyle(.plain)
                    .lineLimit(1...3)
                    .onSubmit(send)
                Button(action: send) {
                    Image(systemName: "arrow.up")
                }
                .buttonStyle(.borderedProminent)
                .buttonBorderShape(.circle)
                .disabled(input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .help("전송 · 미리보기")
            }
            .padding(10)
            .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
            .overlay {
                RoundedRectangle(cornerRadius: 14).strokeBorder(.separator, lineWidth: 0.5)
            }
            Picker("화면 예시", selection: $example) {
                ForEach(Example.allCases) { item in
                    Text(item.rawValue).tag(item)
                }
            }
            .pickerStyle(.segmented)
            .controlSize(.small)
            HStack(spacing: 4) {
                Image(systemName: "eye")
                Text("디자인 미리보기 · 실제 작업은 실행하지 않습니다")
            }
            .font(.system(size: 10)).foregroundStyle(.secondary)
        }
        .padding(12)
        .background(.bar)
    }

    private func send() {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        sent = text
        input = ""
    }
}
