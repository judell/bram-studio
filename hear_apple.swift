// Hear a recording with Apple's on-device SpeechAnalyzer and write its words
// with times as an opinion (the neutral form serve_media.py's write_opinion
// writes). A different lineage from whisper, and fast. Needs macOS 26; the
// speech assets are downloaded on first use.
//
//   hear_apple <in.wav> <out.json>
//
// Prints "done_s=<seconds>" to stderr as each final result arrives (the end
// of its last word), so the server can show progress.
import Foundation
import Speech
import AVFoundation

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write("\(msg)\n".data(using: .utf8)!)
    exit(1)
}

@main
struct HearApple {
    static func main() async {
        let args = CommandLine.arguments
        guard args.count == 3 else { fail("usage: hear_apple <in.wav> <out.json>") }
        let started = Date()
        do {
            let transcriber = SpeechTranscriber(locale: Locale(identifier: "en_US"), transcriptionOptions: [],
                                                reportingOptions: [], attributeOptions: [.audioTimeRange])
            if let req = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                FileHandle.standardError.write("downloading speech assets...\n".data(using: .utf8)!)
                try await req.downloadAndInstall()
            }
            let analyzer = SpeechAnalyzer(modules: [transcriber])
            let file = try AVAudioFile(forReading: URL(fileURLWithPath: args[1]))
            let collect = Task { () -> [[Any]] in
                var words: [[Any]] = []
                for try await r in transcriber.results where r.isFinal {
                    for run in r.text.runs {
                        let text = String(r.text[run.range].characters).trimmingCharacters(in: .whitespacesAndNewlines)
                        if text.isEmpty { continue }
                        if let tr = run.audioTimeRange {
                            words.append([text, tr.start.seconds, tr.end.seconds])
                        } else if var prev = words.last {
                            if text.unicodeScalars.allSatisfy({ CharacterSet.punctuationCharacters.contains($0) }) {
                                prev[0] = (prev[0] as! String) + text
                                words[words.count - 1] = prev
                            } else {
                                let e = prev[2] as! Double
                                words.append([text, e, e])
                            }
                        }
                    }
                    if let end = words.last?[2] as? Double {
                        FileHandle.standardError.write("done_s=\(end)\n".data(using: .utf8)!)
                    }
                }
                return words
            }
            if let last = try await analyzer.analyzeSequence(from: file) {
                try await analyzer.finalizeAndFinish(through: last)
            } else {
                await analyzer.cancelAndFinishNow()
            }
            let words = try await collect.value
            let fmt = DateFormatter()
            fmt.locale = Locale(identifier: "en_US_POSIX")
            fmt.dateFormat = "yyyy-MM-dd HH:mm:ss"
            let secs = (Date().timeIntervalSince(started) * 10).rounded() / 10
            let obj: [String: Any] = ["engine": "apple", "at": fmt.string(from: Date()), "secs": secs, "words": words]
            let data = try JSONSerialization.data(withJSONObject: obj)
            let tmp = URL(fileURLWithPath: args[2] + ".tmp")
            try data.write(to: tmp)
            _ = try FileManager.default.replaceItemAt(URL(fileURLWithPath: args[2]), withItemAt: tmp)
        } catch {
            fail("error: \(error)")
        }
    }
}
