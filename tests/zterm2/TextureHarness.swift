import AppKit
import Metal
import CryptoKit

enum HarnessFailure: Error {
    case failed(String)
}

@main
enum TextureHarness {
    static func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
        if !condition() { throw HarnessFailure.failed(message) }
    }

    static func image(width: Int, height: Int, space: CGColorSpace, alpha: Bool,
                      premultiplied: Bool = true, frame: Int = 0) -> CGImage {
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        for row in 0..<height {
            for column in 0..<width {
                let offset = (row * width + column) * 4
                let opacity = alpha ? (row * 47 + column * 31) % 256 : 255
                let limit = premultiplied ? opacity : 255
                pixels[offset] = UInt8((column * 37 + frame * 13) % (limit + 1))
                pixels[offset + 1] = UInt8((row * 67 + frame * 7) % (limit + 1))
                pixels[offset + 2] = UInt8((column * 11 + row * 23) % (limit + 1))
                pixels[offset + 3] = UInt8(opacity)
            }
        }
        let provider = CGDataProvider(data: Data(pixels) as CFData)!
        return CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32,
                       bytesPerRow: width * 4, space: space,
                       bitmapInfo: CGBitmapInfo(rawValue: premultiplied ? CGImageAlphaInfo.premultipliedLast.rawValue : CGImageAlphaInfo.last.rawValue),
                       provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)!
    }

    static func reference(_ image: CGImage, space: CGColorSpace) -> [UInt8] {
        var bytes = [UInt8](repeating: 0, count: image.width * image.height * 4)
        bytes.withUnsafeMutableBytes { storage in
            let context = CGContext(data: storage.baseAddress, width: image.width, height: image.height,
                                    bitsPerComponent: 8, bytesPerRow: image.width * 4, space: space,
                                    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
            context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
        }
        return bytes
    }

    static func read(_ texture: MTLTexture) -> [UInt8] {
        var bytes = [UInt8](repeating: 0, count: texture.width * texture.height * 4)
        bytes.withUnsafeMutableBytes { storage in
            texture.getBytes(storage.baseAddress!, bytesPerRow: texture.width * 4,
                             from: MTLRegionMake2D(0, 0, texture.width, texture.height), mipmapLevel: 0)
        }
        return bytes
    }

    static func legacy(_ image: CGImage, space: CGColorSpace, device: MTLDevice) -> MTLTexture? {
        let bitmap = NSBitmapImageRep(cgImage: image)
        guard let target = NSColorSpace(cgColorSpace: space),
              let converted = bitmap.converting(to: target, renderingIntent: .default),
              let data = converted.bitmapData else { return nil }
        let descriptor = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm,
                                                                  width: image.width,
                                                                  height: image.height,
                                                                  mipmapped: false)
        guard let texture = device.makeTexture(descriptor: descriptor) else { return nil }
        texture.replace(region: MTLRegionMake2D(0, 0, image.width, image.height), mipmapLevel: 0,
                        withBytes: data, bytesPerRow: converted.bytesPerRow)
        return texture
    }

    static func main() {
        do { try run() } catch {
            fputs("\(error)\n", stderr)
            exit(1)
        }
    }

    static func run() throws {
        guard let device = MTLCreateSystemDefaultDevice(), CommandLine.arguments.count == 3,
              let library = try? device.makeLibrary(URL: URL(fileURLWithPath: CommandLine.arguments[2])),
              let pipeline = ZTermKittyTexturePipeline(device: device, library: library) else {
            throw HarnessFailure.failed("Metal unavailable")
        }
        var spaces = [CGColorSpace(name: CGColorSpace.sRGB)!, CGColorSpace(name: CGColorSpace.displayP3)!]
        let mode = CommandLine.arguments.dropFirst().first ?? "test"
        var report: [String: Any] = ["mode": mode, "device": device.name, "kind": "isolated-microbenchmark"]
        if mode == "test" {
            if let profile = NSScreen.main?.colorSpace?.cgColorSpace {
                spaces.append(profile)
                if let data = profile.copyICCData() as Data? {
                    report["display_profile_sha256"] = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
                }
            }
            var cases = [[String: Any]]()
            for (sourceIndex, sourceSpace) in spaces.enumerated() {
                for (targetIndex, targetSpace) in spaces.enumerated() {
                    for alpha in [false, true] {
                        for premultiplied in [false, true] {
                        try autoreleasepool {
                            let input = image(width: 8, height: 6, space: sourceSpace,
                                              alpha: alpha, premultiplied: premultiplied)
                            let generation = UUID()
                            guard let lease = pipeline.convert(input, colorSpace: targetSpace, generation: generation) else {
                                throw HarnessFailure.failed("conversion failed")
                            }
                            try require(lease.generation == generation, "stale content generation")
                            let actual = read(lease.texture)
                            let expected = reference(input, space: targetSpace)
                            let difference = zip(actual, expected).map { abs(Int($0) - Int($1)) }.max()!
                            let cpu = legacy(input, space: targetSpace, device: device)!
                            let legacyDifference = zip(read(cpu), expected).map { abs(Int($0) - Int($1)) }.max()!
                            cases.append(["source": sourceIndex, "target": targetIndex, "alpha": alpha,
                                          "premultiplied": premultiplied,
                                          "maximum_byte_error": difference,
                                          "legacy_maximum_byte_error": legacyDifference])
                            try require(difference <= 4, "color/orientation mismatch: \(cases.last!)")
                            if premultiplied || !alpha {
                                try require(legacyDifference <= 4, "legacy model mismatch: \(cases.last!)")
                            }
                        }
                        }
                    }
                }
            }
            let pool = ZTermTexturePool(device: device, maximumCount: 2, maximumBytes: 1024 * 1024)
            var probe = [UInt8](repeating: 255, count: 256 * 256 * 4)
            for index in 0..<(256 * 256) {
                probe[index * 4] = UInt8(index % 256)
                probe[index * 4 + 1] = UInt8((index / 256 % 16) * 17)
                probe[index * 4 + 2] = UInt8((index / 4096) * 17)
            }
            var gridErrors = [Int]()
            for source in spaces {
                for target in spaces {
                    try autoreleasepool {
                        let image = CGImage(width: 256, height: 256, bitsPerComponent: 8, bitsPerPixel: 32,
                                            bytesPerRow: 1024, space: source,
                                            bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.last.rawValue),
                                            provider: CGDataProvider(data: Data(probe) as CFData)!,
                                            decode: nil, shouldInterpolate: false, intent: .defaultIntent)!
                        let lease = pipeline.convert(image, colorSpace: target, generation: UUID())!
                        let error = zip(read(lease.texture), reference(image, space: target))
                            .map { abs(Int($0) - Int($1)) }.max()!
                        gridErrors.append(error)
                        try require(error <= 2, "dense gamut grid mismatch: \(gridErrors)")
                    }
                }
            }
            report["dense_grid_maximum_errors"] = gridErrors
            var held = pool.acquire(width: 16, height: 16, generation: UUID())
            let firstIdentity = ObjectIdentifier(held!.texture)
            var second = pool.acquire(width: 16, height: 16, generation: UUID())
            try require(second != nil, "second slot unavailable")
            try require(pool.acquire(width: 16, height: 16, generation: UUID()) == nil, "pool exceeded count")
            held = nil
            var recycled = pool.acquire(width: 16, height: 16, generation: UUID())
            try require(ObjectIdentifier(recycled!.texture) == firstIdentity, "compatible texture not reused")
            pool.drain()
            try require(pool.allocatedCount == 2, "drain released in-use textures")
            second = nil
            recycled = nil
            try require(pool.allocatedCount == 0 && pool.allocatedBytes == 0, "drain leaked textures")
            try require(pool.acquire(width: 4097, height: 1, generation: UUID()) == nil, "oversize accepted")
            let tiny = ZTermTexturePool(device: device, maximumBytes: 64)
            try require(tiny.acquire(width: 32, height: 32, generation: UUID()) == nil, "byte budget exceeded")
            let queue = device.makeCommandQueue()!
            let event = device.makeSharedEvent()!
            let command = queue.makeCommandBuffer()!
            var retained = pool.acquire(width: 16, height: 16, generation: UUID())
            let flight = ZTermTextureFrame()
            flight.retain(retained!)
            let completed = DispatchSemaphore(value: 0)
            command.encodeWaitForEvent(event, value: 1)
            command.addCompletedHandler { _ in
                flight.complete()
                completed.signal()
            }
            retained = nil
            command.commit()
            pool.drain()
            try require(pool.allocatedCount == 1, "GPU lease was recycled early")
            event.signaledValue = 1
            command.waitUntilCompleted()
            try require(completed.wait(timeout: .now() + 2) == .success, "GPU completion not delivered")
            try require(pool.allocatedCount == 0, "GPU completion retained retired lease")
            // Exercise the application path: conversion and its consumer share one
            // command buffer, with no conversion-side commit or CPU wait.
            let asyncCommand = queue.makeCommandBuffer()!
            let asyncEvent = device.makeSharedEvent()!
            asyncCommand.encodeWaitForEvent(asyncEvent, value: 1)
            let asyncImage = image(width: 64, height: 32, space: spaces[0], alpha: true)
            var asyncLease = pipeline.encode(asyncImage, colorSpace: spaces[1], generation: UUID(),
                                             commandBuffer: asyncCommand)
            try require(asyncLease != nil, "asynchronous conversion failed")
            let destination = device.makeBuffer(length: 64 * 32 * 4, options: .storageModeShared)!
            let blit = asyncCommand.makeBlitCommandEncoder()!
            blit.copy(from: asyncLease!.texture, sourceSlice: 0, sourceLevel: 0,
                      sourceOrigin: MTLOrigin(x: 0, y: 0, z: 0), sourceSize: MTLSize(width: 64, height: 32, depth: 1),
                      to: destination, destinationOffset: 0, destinationBytesPerRow: 256,
                      destinationBytesPerImage: 64 * 32 * 4)
            blit.endEncoding()
            asyncLease = nil
            pipeline.drain()
            try require(pipeline.pool.allocatedCount == 1, "encoded output recycled before submission")
            asyncCommand.commit()
            asyncEvent.signaledValue = 1
            asyncCommand.waitUntilCompleted()
            try require(asyncCommand.status == .completed, "asynchronous command failed")
            let actualAsync = Array(UnsafeBufferPointer(start: destination.contents().assumingMemoryBound(to: UInt8.self),
                                                       count: 64 * 32 * 4))
            try require(zip(actualAsync, reference(asyncImage, space: spaces[1]))
                .allSatisfy { abs(Int($0) - Int($1)) <= 4 }, "same-command-buffer consumer saw stale pixels")
            report["same_command_buffer_consumer"] = "passed"
            for frame in 0..<12 {
                try autoreleasepool {
                    let input = image(width: 8 + frame % 3, height: 6, space: spaces[0], alpha: true, frame: frame)
                    let lease = pipeline.convert(input, colorSpace: spaces[1], generation: UUID())!
                    let difference = zip(read(lease.texture), reference(input, space: spaces[1]))
                        .map { abs(Int($0) - Int($1)) }.max()!
                    try require(difference <= 4, "resize/stale frame mismatch")
                }
            }
            pipeline.drain()
            try require(pipeline.pool.allocatedCount == 0, "pipeline drain retained idle textures")
            report["color_cases"] = cases
            report["lifecycle"] = "passed"
            try require(pipeline.lookupConversions > 0 && pipeline.coreImageConversions > 0,
                        "fast path and colour-preserving fallback were not both exercised")
            report["lookup_conversions"] = pipeline.lookupConversions
            report["core_image_conversions"] = pipeline.coreImageConversions
        } else {
            try require(["legacy", "gpu"].contains(mode), "unknown benchmark mode")
            let start = ProcessInfo.processInfo.systemUptime
            var checksum = 0
            var fixtureHash = SHA256()
            for frame in 0..<90 {
                try autoreleasepool {
                    let input = image(width: 640, height: 360, space: spaces[0], alpha: false, frame: frame)
                    fixtureHash.update(data: input.dataProvider!.data! as Data)
                    let texture: MTLTexture
                    if mode == "legacy" {
                        guard let result = legacy(input, space: spaces[1], device: device) else {
                            throw HarnessFailure.failed("legacy conversion failed")
                        }
                        texture = result
                    } else {
                        guard let lease = pipeline.convert(input, colorSpace: spaces[1], generation: UUID()) else {
                            throw HarnessFailure.failed("GPU conversion failed")
                        }
                        texture = lease.texture
                    }
                    checksum += Int(read(texture)[0])
                }
                let deadline = start + Double(frame + 1) / 30
                let remaining = deadline - ProcessInfo.processInfo.systemUptime
                if remaining > 0 { Thread.sleep(forTimeInterval: remaining) }
            }
            report["frames"] = 90
            report["seconds"] = ProcessInfo.processInfo.systemUptime - start
            report["checksum"] = checksum
            report["fixture_sha256"] = fixtureHash.finalize().map { String(format: "%02x", $0) }.joined()
            report["pool_count"] = pipeline.pool.allocatedCount
            report["geometry"] = "640x360"
            report["cadence"] = 30
            report["source_space"] = "sRGB"
            report["destination_space"] = "Display P3"
        }
        let data = try JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
        print(String(decoding: data, as: UTF8.self))
    }
}
