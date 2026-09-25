// Record from a named input device with AVFoundation's own file writer,
// bypassing ffmpeg's avfoundation capture. Used by voicetest.py.
//
//   record_native <device name> <out.wav> <seconds>
//
// seconds = 0 records until SIGTERM or SIGINT (record.sh's session mode).
//
// Prints "started <epoch>" when samples begin flowing and "stopped <epoch>"
// when the file is closed.
import AVFoundation
import Foundation

let args = CommandLine.arguments
guard args.count == 4, let seconds = Double(args[3]) else {
    FileHandle.standardError.write("usage: record_native <device> <out.wav> <seconds>\n".data(using: .utf8)!)
    exit(2)
}
let discovery = AVCaptureDevice.DiscoverySession(deviceTypes: [.microphone, .external],
                                                 mediaType: .audio, position: .unspecified)
guard let device = discovery.devices.first(where: { $0.localizedName == args[1] }) else {
    FileHandle.standardError.write("no audio device named \(args[1])\n".data(using: .utf8)!)
    exit(1)
}

class Delegate: NSObject, AVCaptureFileOutputRecordingDelegate {
    let seconds: Double
    init(seconds: Double) { self.seconds = seconds }
    func fileOutput(_ output: AVCaptureFileOutput, didStartRecordingTo url: URL, from connections: [AVCaptureConnection]) {
        print("started \(Date().timeIntervalSince1970)"); fflush(stdout)
        if seconds > 0 {
            DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { output.stopRecording() }
        }
    }
    func fileOutput(_ output: AVCaptureFileOutput, didFinishRecordingTo url: URL, from connections: [AVCaptureConnection], error: Error?) {
        print("stopped \(Date().timeIntervalSince1970)"); fflush(stdout)
        if let error = error { FileHandle.standardError.write("\(error)\n".data(using: .utf8)!) }
        exit(error == nil ? 0 : 1)
    }
}

let session = AVCaptureSession()
let output = AVCaptureAudioFileOutput()
output.audioSettings = [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 48000, AVNumberOfChannelsKey: 1,
                        AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false, AVLinearPCMIsBigEndianKey: false]
session.addInput(try AVCaptureDeviceInput(device: device))
session.addOutput(output)
session.startRunning()
let delegate = Delegate(seconds: seconds)
let url = URL(fileURLWithPath: args[2])
try? FileManager.default.removeItem(at: url)
output.startRecording(to: url, outputFileType: .wav, recordingDelegate: delegate)
// On SIGTERM/SIGINT, stop through the writer so the WAV header is finalized.
var stoppers: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { output.stopRecording() }
    src.resume()
    stoppers.append(src)
}
RunLoop.main.run()
