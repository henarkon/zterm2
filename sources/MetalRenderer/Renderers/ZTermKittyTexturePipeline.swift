import Foundation
import CoreImage
import Metal
import CryptoKit

final class ZTermRenderMetrics {
    let enabled = ProcessInfo.processInfo.environment["ZTERM2_RENDER_METRICS"] == "1"
    private var gpuUploads = 0
    private var cpuUploads = 0

    func record(gpu: Bool, pool: ZTermTexturePool?, profile: Data?, lookup: Int = 0, coreImage: Int = 0,
                lookupBuilds: Int = 0, lookupBytes: Int = 0, viewportWidth: Int = 0, viewportHeight: Int = 0,
                scale: Double = 0) {
        guard enabled else { return }
        if gpu { gpuUploads += 1 } else { cpuUploads += 1 }
        let total = gpuUploads + cpuUploads
        guard total == 1 || total % 30 == 0 else { return }
        let profileHash = profile.map { SHA256.hash(data: $0).map { String(format: "%02x", $0) }.joined() } ?? "unknown"
        let values: [String: Any] = ["gpu_uploads": gpuUploads, "cpu_uploads": cpuUploads,
                                    "pool_count": pool?.allocatedCount ?? 0,
                                    "pool_bytes": pool?.allocatedBytes ?? 0, "output_profile_sha256": profileHash,
                                    "lookup_conversions": lookup, "core_image_conversions": coreImage,
                                    "lookup_builds": lookupBuilds, "lookup_bytes": lookupBytes,
                                    "viewport_width": viewportWidth, "viewport_height": viewportHeight, "scale": scale]
        guard let json = try? JSONSerialization.data(withJSONObject: values, options: .sortedKeys) else { return }
        FileHandle.standardError.write(Data("ZTERM2_METRICS ".utf8) + json + Data([10]))
    }
}

final class ZTermTextureLease {
    let texture: MTLTexture
    let upload: MTLBuffer?
    let generation: UUID
    private let releaseTexture: (() -> Void)?

    init(texture: MTLTexture, generation: UUID, upload: MTLBuffer? = nil, releaseTexture: (() -> Void)? = nil) {
        self.texture = texture
        self.upload = upload
        self.generation = generation
        self.releaseTexture = releaseTexture
    }

    deinit {
        releaseTexture?()
    }
}

final class ZTermTexturePool {
    private let device: MTLDevice
    private let lock = NSLock()
    private let maximumCount: Int
    private let maximumBytes: Int
    private struct Storage {
        let texture: MTLTexture
        let upload: MTLBuffer
        var allocatedSize: Int { texture.allocatedSize + upload.allocatedSize }
    }
    private var idle = [Storage]()
    private var count = 0
    private var bytes = 0
    private var epoch = 0

    init(device: MTLDevice, maximumCount: Int = 4, maximumBytes: Int = 64 * 1024 * 1024) {
        self.device = device
        self.maximumCount = maximumCount
        self.maximumBytes = maximumBytes
    }

    var allocatedCount: Int {
        lock.lock()
        defer { lock.unlock() }
        return count
    }

    var allocatedBytes: Int {
        lock.lock()
        defer { lock.unlock() }
        return bytes
    }

    func acquire(width: Int, height: Int, generation: UUID) -> ZTermTextureLease? {
        guard width > 0, height > 0, width <= 4096, height <= 4096 else { return nil }
        let requiredBytes = width * height * 8
        lock.lock()
        defer { lock.unlock() }
        let storage: Storage
        if let index = idle.firstIndex(where: { $0.texture.width == width && $0.texture.height == height }) {
            storage = idle.remove(at: index)
        } else {
            while !idle.isEmpty && (count >= maximumCount || bytes + requiredBytes > maximumBytes) {
                let removed = idle.removeLast()
                count -= 1
                bytes -= removed.allocatedSize
            }
            guard count < maximumCount, bytes + requiredBytes <= maximumBytes else { return nil }
            let descriptor = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm,
                                                                      width: width,
                                                                      height: height,
                                                                      mipmapped: false)
            descriptor.usage = [.shaderRead, .shaderWrite, .renderTarget]
            descriptor.storageMode = .shared
            guard let texture = device.makeTexture(descriptor: descriptor),
                  let upload = device.makeBuffer(length: width * height * 4, options: .storageModeShared) else { return nil }
            let created = Storage(texture: texture, upload: upload)
            guard bytes + created.allocatedSize <= maximumBytes else { return nil }
            storage = created
            count += 1
            bytes += storage.allocatedSize
        }
        let acquiredEpoch = epoch
        return ZTermTextureLease(texture: storage.texture, generation: generation, upload: storage.upload) { [self] in
            lock.lock()
            defer { lock.unlock() }
            if acquiredEpoch == epoch {
                idle.append(storage)
            } else {
                count -= 1
                bytes -= storage.allocatedSize
            }
        }
    }

    func drain() {
        lock.lock()
        defer { lock.unlock() }
        epoch += 1
        count -= idle.count
        bytes -= idle.reduce(0) { $0 + $1.allocatedSize }
        idle.removeAll()
    }
}

final class ZTermTextureFrame {
    private let lock = NSLock()
    private var leases = [ZTermTextureLease]()

    func retain(_ lease: ZTermTextureLease) {
        lock.lock()
        defer { lock.unlock() }
        leases.append(lease)
    }

    func complete() {
        lock.lock()
        defer { lock.unlock() }
        leases.removeAll()
    }
}

final class ZTermKittyTexturePipeline {
    let pool: ZTermTexturePool
    private let device: MTLDevice
    private var standaloneQueue: MTLCommandQueue?
    private let library: MTLLibrary?
    private var context: CIContext?
    private var conversionPipeline: MTLRenderPipelineState?
    private var lookup: MTLTexture?
    private var sourceSpace: CGColorSpace?
    private var destinationSpace: CGColorSpace?
    private(set) var lookupConversions = 0
    private(set) var coreImageConversions = 0
    private(set) var lookupBuilds = 0
    var lookupBytes: Int { lookup?.allocatedSize ?? 0 }

    init?(device: MTLDevice, library: MTLLibrary? = nil) {
        self.device = device
        self.library = library
        pool = ZTermTexturePool(device: device)
    }

    func convert(_ image: CGImage, colorSpace: CGColorSpace, generation: UUID) -> ZTermTextureLease? {
        if standaloneQueue == nil { standaloneQueue = device.makeCommandQueue() }
        guard let commandBuffer = standaloneQueue?.makeCommandBuffer(),
              let lease = encode(image, colorSpace: colorSpace, generation: generation,
                                 commandBuffer: commandBuffer) else { return nil }
        commandBuffer.commit()
        commandBuffer.waitUntilCompleted()
        guard commandBuffer.status == .completed else { return nil }
        return lease
    }

    // The caller must encode this before opening its render encoder. The tracked
    // texture dependency then orders conversion and sampling on the GPU without a CPU wait.
    func encode(_ image: CGImage, colorSpace: CGColorSpace, generation: UUID,
                commandBuffer: MTLCommandBuffer) -> ZTermTextureLease? {
        guard image.colorSpace?.model == .rgb, colorSpace.model == .rgb,
              image.bitsPerComponent == 8,
              let lease = pool.acquire(width: image.width, height: image.height, generation: generation) else { return nil }
        if encodeLookup(image, colorSpace: colorSpace, lease: lease, commandBuffer: commandBuffer) {
            lookupConversions += 1
            commandBuffer.addCompletedHandler { _ in _ = lease }
            return lease
        }
        if context == nil {
            var options: [CIContextOption: Any] = [.cacheIntermediates: false, .useSoftwareRenderer: false]
            if #available(macOS 14.0, *) { options[.memoryTarget] = 8 }
            context = CIContext(mtlCommandQueue: commandBuffer.commandQueue, options: options)
        }
        let input = CIImage(cgImage: image).transformed(by:
            CGAffineTransform(translationX: 0, y: CGFloat(image.height)).scaledBy(x: 1, y: -1))
        context?.render(input, to: lease.texture, commandBuffer: commandBuffer,
                       bounds: input.extent, colorSpace: colorSpace)
        coreImageConversions += 1
        // Keep the output unavailable for recycling even if the caller drops its
        // cache before this command buffer has finished (including aborted frames).
        commandBuffer.addCompletedHandler { _ in
            _ = lease
            _ = input
        }
        return lease
    }

    private func encodeLookup(_ image: CGImage, colorSpace: CGColorSpace, lease: ZTermTextureLease,
                              commandBuffer: MTLCommandBuffer) -> Bool {
        let order = image.bitmapInfo.intersection(.byteOrderMask)
        guard image.bitsPerPixel == 32, image.decode == nil,
              image.bytesPerRow == image.width * 4,
              order == .byteOrder32Big || order == .byteOrderDefault,
              [.last, .premultipliedLast].contains(image.alphaInfo),
              let source = image.colorSpace, let upload = lease.upload,
              source == CGColorSpace(name: CGColorSpace.sRGB),
              let data = image.dataProvider?.data,
              CFDataGetLength(data) >= image.bytesPerRow * image.height else { return false }
        if conversionPipeline == nil {
            guard let library = library ?? device.makeDefaultLibrary(),
                  let vertex = library.makeFunction(name: "ZTermKittyConvertVertex"),
                  let fragment = library.makeFunction(name: "ZTermKittyConvertFragment") else { return false }
            let descriptor = MTLRenderPipelineDescriptor()
            descriptor.vertexFunction = vertex
            descriptor.fragmentFunction = fragment
            descriptor.colorAttachments[0].pixelFormat = .rgba8Unorm
            guard let state = try? device.makeRenderPipelineState(descriptor: descriptor) else { return false }
            conversionPipeline = state
        }
        if sourceSpace != source || destinationSpace != colorSpace || lookup == nil {
            guard let created = makeLookup(source: source, destination: colorSpace) else { return false }
            lookup = created
            lookupBuilds += 1
            sourceSpace = source
            destinationSpace = colorSpace
        }
        let pass = MTLRenderPassDescriptor()
        pass.colorAttachments[0].texture = lease.texture
        pass.colorAttachments[0].loadAction = .dontCare
        pass.colorAttachments[0].storeAction = .store
        guard let conversionPipeline, let lookup,
              let encoder = commandBuffer.makeRenderCommandEncoder(descriptor: pass) else { return false }
        memcpy(upload.contents(), CFDataGetBytePtr(data), image.bytesPerRow * image.height)
        var shape = SIMD4<UInt32>(UInt32(image.width), UInt32(image.height),
                                  image.alphaInfo == .premultipliedLast ? 1 : 0, 0)
        encoder.setRenderPipelineState(conversionPipeline)
        encoder.setFragmentBuffer(upload, offset: 0, index: 0)
        encoder.setFragmentBytes(&shape, length: MemoryLayout.size(ofValue: shape), index: 1)
        encoder.setFragmentTexture(lookup, index: 0)
        encoder.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 3)
        encoder.endEncoding()
        return true
    }

    private func makeLookup(source: CGColorSpace, destination: CGColorSpace) -> MTLTexture? {
        // Canonical Kitty input is sRGB. Other input spaces retain the exact CI
        // path, avoiding LUT interpolation across out-of-gamut clipping boundaries.
        // One bounded 64^3 table (2 MiB), replaced when either ICC profile changes.
        // Float input avoids quantising grid points before Core Graphics colour matching.
        let size = 64
        let flags = CGBitmapInfo.floatComponents.union(.byteOrder32Little)
            .union(CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue))
        let descriptor = MTLTextureDescriptor()
        descriptor.textureType = .type3D
        descriptor.pixelFormat = .rgba16Float
        descriptor.width = size
        descriptor.height = size
        descriptor.depth = size
        descriptor.usage = .shaderRead
        descriptor.storageMode = .shared
        guard let texture = device.makeTexture(descriptor: descriptor) else { return nil }
        // Build one blue plane at a time: less than 1 MiB of CPU workspace,
        // instead of simultaneous full-volume float buffers exceeding 100 MiB.
        var input = [Float](repeating: 1, count: size * size * 4)
        var converted = [Float](repeating: 0, count: input.count)
        for blue in 0..<size {
            let succeeded = autoreleasepool { () -> Bool in
                for green in 0..<size {
                    for red in 0..<size {
                        let offset = (green * size + red) * 4
                        input[offset] = Float(red) / Float(size - 1)
                        input[offset + 1] = Float(green) / Float(size - 1)
                        input[offset + 2] = Float(blue) / Float(size - 1)
                    }
                }
                guard let provider = CGDataProvider(data: input.withUnsafeBytes { Data($0) } as CFData),
                      let image = CGImage(width: size, height: size, bitsPerComponent: 32, bitsPerPixel: 128,
                                          bytesPerRow: size * 16, space: source, bitmapInfo: flags,
                                          provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent) else { return false }
                let matched = converted.withUnsafeMutableBytes { data -> Bool in
                    guard let context = CGContext(data: data.baseAddress, width: size, height: size,
                                                  bitsPerComponent: 32, bytesPerRow: size * 16,
                                                  space: destination, bitmapInfo: flags.rawValue) else { return false }
                    context.draw(image, in: CGRect(x: 0, y: 0, width: size, height: size))
                    return true
                }
                guard matched else { return false }
                let half = converted.map { Float16($0) }
                half.withUnsafeBytes { data in
                    texture.replace(region: MTLRegionMake3D(0, 0, blue, size, size, 1), mipmapLevel: 0, slice: 0,
                                    withBytes: data.baseAddress!, bytesPerRow: size * 8, bytesPerImage: size * size * 8)
                }
                return true
            }
            if !succeeded { return nil }
        }
        return texture
    }

    func drain() {
        pool.drain()
        context?.clearCaches()
        context = nil
        lookup = nil
        sourceSpace = nil
        destinationSpace = nil
    }
}
