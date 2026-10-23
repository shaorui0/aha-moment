// Aha OCR: Apple Vision VNRecognizeTextRequest (accurate) -> JSON lines on stdout.
// usage: ocr <image> [--langs ja-JP,zh-Hans,en-US]
// each line: {"text": "...", "confidence": 0.98, "box": [x, y, w, h]}   (normalised, origin bottom-left; lines sorted top->bottom)
import Vision
import AppKit

var args = Array(CommandLine.arguments.dropFirst())
var langs = ["ja-JP", "zh-Hans", "en-US"]
if let i = args.firstIndex(of: "--langs"), i + 1 < args.count {
    langs = args[i + 1].split(separator: ",").map(String.init)
    args.removeSubrange(i...(i + 1))
}
guard let path = args.first else {
    FileHandle.standardError.write("usage: ocr <image> [--langs ja-JP,zh-Hans,en-US]\n".data(using: .utf8)!)
    exit(2)
}
var rect = CGRect.zero
guard let img = NSImage(contentsOfFile: path), let cg = img.cgImage(forProposedRect: &rect, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot read image: \(path)\n".data(using: .utf8)!)
    exit(1)
}
let req = VNRecognizeTextRequest()
req.recognitionLevel = .accurate
req.recognitionLanguages = langs
req.usesLanguageCorrection = true
do { try VNImageRequestHandler(cgImage: cg).perform([req]) } catch {
    FileHandle.standardError.write("vision failed: \(error)\n".data(using: .utf8)!)
    exit(1)
}
let obs = (req.results ?? []).sorted { $0.boundingBox.midY > $1.boundingBox.midY }
for o in obs {
    guard let c = o.topCandidates(1).first else { continue }
    let b = o.boundingBox
    let line: [String: Any] = ["text": c.string, "confidence": Double(c.confidence),
                               "box": [b.minX, b.minY, b.width, b.height]]
    if let d = try? JSONSerialization.data(withJSONObject: line), let s = String(data: d, encoding: .utf8) { print(s) }
}
