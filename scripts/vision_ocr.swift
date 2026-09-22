#!/usr/bin/env swift

import AppKit
import Foundation
import Vision

struct OCRLine: Codable {
    let text: String
    let confidence: Float
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

guard CommandLine.arguments.count >= 2 else {
    fail("usage: vision_ocr.swift IMAGE [--text]")
}

let imagePath = CommandLine.arguments[1]
let textOnly = CommandLine.arguments.contains("--text")
guard let nsImage = NSImage(contentsOfFile: imagePath) else {
    fail("cannot open image: \(imagePath)")
}

var rect = NSRect(origin: .zero, size: nsImage.size)
guard let cgImage = nsImage.cgImage(forProposedRect: &rect, context: nil, hints: nil) else {
    fail("cannot create CGImage: \(imagePath)")
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.minimumTextHeight = 0.006

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
} catch {
    fail("Vision OCR failed: \(error)")
}

let observations = (request.results ?? []).sorted { lhs, rhs in
    let verticalDelta = lhs.boundingBox.midY - rhs.boundingBox.midY
    if abs(verticalDelta) > 0.008 {
        return verticalDelta > 0
    }
    return lhs.boundingBox.minX < rhs.boundingBox.minX
}

let lines: [OCRLine] = observations.compactMap { observation in
    guard let candidate = observation.topCandidates(1).first else { return nil }
    let box = observation.boundingBox
    return OCRLine(
        text: candidate.string,
        confidence: candidate.confidence,
        x: box.minX,
        y: box.minY,
        width: box.width,
        height: box.height
    )
}

if textOnly {
    for line in lines {
        print(line.text)
    }
} else {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    let data = try encoder.encode(lines)
    print(String(data: data, encoding: .utf8)!)
}
