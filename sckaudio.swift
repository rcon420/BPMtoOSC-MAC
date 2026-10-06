// sckaudio: captures the Mac's system audio with ScreenCaptureKit (macOS 13+)
// and writes it to stdout as raw mono float32 samples at 48000 Hz.
// Build: swiftc -O -swift-version 5 sckaudio.swift -o sckaudio

import Foundation
import ScreenCaptureKit
import CoreMedia
import AudioToolbox

signal(SIGPIPE, SIG_DFL)  // stop automatically when the app closes the pipe

final class AudioOutput: NSObject, SCStreamOutput, SCStreamDelegate {

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }
        guard let fmt = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(fmt) else { return }
        let asbd = asbdPtr.pointee

        // only 32 bit float audio is expected from ScreenCaptureKit
        guard (asbd.mFormatFlags & kAudioFormatFlagIsFloat) != 0, asbd.mBitsPerChannel == 32 else { return }
        let channels = max(1, Int(asbd.mChannelsPerFrame))
        let nonInterleaved = (asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0

        // 1st call: how big must the AudioBufferList be?
        var sizeNeeded = 0
        _ = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: &sizeNeeded,
            bufferListOut: nil,
            bufferListSize: 0,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: nil)
        guard sizeNeeded > 0 else { return }

        let raw = UnsafeMutableRawPointer.allocate(byteCount: sizeNeeded, alignment: 16)
        defer { raw.deallocate() }
        let abl = raw.bindMemory(to: AudioBufferList.self, capacity: 1)

        // 2nd call: fill it
        var blockBuffer: CMBlockBuffer?
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: abl,
            bufferListSize: sizeNeeded,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &blockBuffer)
        guard status == noErr else { return }

        let buffers = UnsafeMutableAudioBufferListPointer(abl)
        guard buffers.count > 0 else { return }

        var mono: [Float] = []

        if nonInterleaved {
            // one buffer per channel -> average them
            let frames = Int(buffers[0].mDataByteSize) / 4
            mono = [Float](repeating: 0, count: frames)
            var used = 0
            for b in buffers {
                guard let data = b.mData else { continue }
                let p = data.assumingMemoryBound(to: Float.self)
                let n = min(frames, Int(b.mDataByteSize) / 4)
                for i in 0..<n { mono[i] += p[i] }
                used += 1
            }
            if used > 1 {
                let k = 1.0 / Float(used)
                for i in 0..<frames { mono[i] *= k }
            }
        } else {
            // channels are interleaved in the first buffer
            guard let data = buffers[0].mData else { return }
            let p = data.assumingMemoryBound(to: Float.self)
            let frames = Int(buffers[0].mDataByteSize) / (4 * channels)
            mono = [Float](repeating: 0, count: frames)
            for i in 0..<frames {
                var sum: Float = 0
                for c in 0..<channels { sum += p[i * channels + c] }
                mono[i] = sum / Float(channels)
            }
        }

        mono.withUnsafeBufferPointer { ptr in
            _ = fwrite(ptr.baseAddress, 4, ptr.count, stdout)
        }
        fflush(stdout)
        withExtendedLifetime(blockBuffer) {}
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fputs("stream stopped: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
}

var keepOutput: AudioOutput?
var keepStream: SCStream?

Task {
    do {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else {
            fputs("error: no display found\n", stderr)
            exit(2)
        }

        let filter = SCContentFilter(display: display, excludingWindows: [])
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48000
        config.channelCount = 2
        // we only want audio, so keep the (unused) video as tiny and slow as possible
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        let output = AudioOutput()
        let stream = SCStream(filter: filter, configuration: config, delegate: output)
        try stream.addStreamOutput(output, type: .audio, sampleHandlerQueue: DispatchQueue(label: "sckaudio.audio"))
        try await stream.startCapture()

        keepOutput = output
        keepStream = stream
        fputs("started\n", stderr)
    } catch {
        fputs("error: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
}

RunLoop.main.run()
