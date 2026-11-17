//
//  KittyImageRenderer.swift
//  iTerm2SharedARC
//
//  Created by George Nachman on 8/26/24.
//

import Foundation

// A sequence of draws to perform based on adjacent unicode placeholders.
@objc(iTermKittyImageRun)
class KittyImageRun: NSObject {
    let draw: iTermKittyImageDraw
    let sourceCoord: VT100GridCoord
    let destCoord: VT100GridCoord
    @objc var length: UInt

    override var description: String {
        return "<KittyImageRun: \(it_addressString) source=\(VT100GridCoordDescription(sourceCoord)) dest=\(VT100GridCoordDescription(destCoord)) draw=\(draw.description)>"
    }
    @objc(initWithDraw:sourceCoord:destCoord:length:)
    init(draw: iTermKittyImageDraw,
         sourceCoord: VT100GridCoord,
         destCoord: VT100GridCoord,
         length: UInt) {
        self.draw = draw
        self.sourceCoord = sourceCoord
        self.destCoord = destCoord
        self.length = length
    }
}

@objc(iTermKittyImageRendererTransientState)
class KittyImageRendererTransientState: iTermMetalCellRendererTransientState {
    fileprivate var draws = [iTermKittyImageDraw]()
    fileprivate var textures = ReferenceContainer<[UUID: ZTermTextureLease]>([:])
    fileprivate var frameLeases = ZTermTextureFrame()
    fileprivate var texturePipeline: ZTermKittyTexturePipeline?
    fileprivate var metrics: ZTermRenderMetrics?
    // The renderer draws this transient state once per z-range (three times per frame), all sharing
    // the same draws. Prune the shared texture cache only on the first pass of the frame.
    fileprivate var didPruneTextures = false
    fileprivate var cellRenderer: iTermMetalCellRenderer!
    @objc var visibleRect = NSRect.zero
    @objc var totalScrollbackOverflow = Int64(0)
    fileprivate var runs = [KittyImageRun]()

    private var scaledVisibleRect: NSRect {
        visibleRect * configuration.scale
    }

    @objc(addDraw:)
    func add(draw: iTermKittyImageDraw) {
        DLog("KittyImageRendererTransientState.add(draw:): imageID=\(draw.imageID) (0x\(String(draw.imageID, radix: 16))) placementID=\(draw.placementID) virtual=\(draw.virtual)")
        draws.append(draw)
    }

    @objc(addRuns:)
    func add(runs: [KittyImageRun]) {
        DLog("KittyImageRendererTransientState.add(runs:): adding \(runs.count) runs")
        for (i, run) in runs.enumerated() {
            DLog("  run[\(i)]: imageID=\(run.draw.imageID) (0x\(String(run.draw.imageID, radix: 16))) sourceCoord=\(VT100GridCoordDescription(run.sourceCoord)) destCoord=\(VT100GridCoordDescription(run.destCoord)) length=\(run.length)")
        }
        self.runs.append(contentsOf: runs)
    }

    struct Operation {
        var texture: MTLTexture
        var source: NSRect
        var destination: NSRect
        var quad: MTLBuffer
    }

    private struct Geometry {
        var draw: iTermKittyImageDraw
        var frame: NSRect
        var textureFrame: NSRect
        var source: NSRect
        var destination: NSRect
    }

    fileprivate var visibleImageIDs: Set<UUID> {
        var result = Set<UUID>()
        enumerateGeometries(zRange: Int(Int32.min)..<(Int(Int32.max) + 1)) { geometry in
            result.insert(geometry.draw.imageUniqueID)
        }
        return result
    }

    fileprivate func enumerateOperations(zRange: Range<Int>, closure: (Operation) -> ()) {
        enumerateGeometries(zRange: zRange) { geometry in
            guard let texture = texture(for: geometry.draw) else { return }
            let quad = cellRenderer.newQuad(withFrame: geometry.frame,
                                             textureFrame: geometry.textureFrame,
                                             poolContext: poolContext)
            closure(Operation(texture: texture, source: geometry.source,
                              destination: geometry.destination, quad: quad))
        }
    }

    private func enumerateGeometries(zRange: Range<Int>, closure: (Geometry) -> ()) {
        DLog("enumerateOperations: zRange=\(zRange) draws.count=\(draws.count) runs.count=\(runs.count)")
        let screenOrigin = scaledVisibleRect.origin.addingY(-configuration.extraMargins.top)
        for (i, draw) in draws.enumerated() {
            guard zRange.contains(Int(clamping: draw.zIndex)) else {
                DLog("enumerateOperations: draw[\(i)] imageID=\(draw.imageID) - zIndex \(draw.zIndex) not in range, skipping")
                continue
            }
            if draw.virtual {
                DLog("enumerateOperations: draw[\(i)] imageID=\(draw.imageID) - virtual, skipping (handled via runs)")
                continue
            }
            var shiftedDestinationFrame = draw.destinationFrame
            shiftedDestinationFrame.shiftY(by: cellConfiguration.cellSize.height * CGFloat(-totalScrollbackOverflow))
            guard shiftedDestinationFrame.intersects(scaledVisibleRect) else {
                DLog("enumerateOperations: draw[\(i)] imageID=\(draw.imageID) - not visible, skipping")
                continue
            }
            DLog("enumerateOperations: draw[\(i)] imageID=\(draw.imageID) (0x\(String(draw.imageID, radix: 16))) - rendering")
            let destinationFrame = shiftedDestinationFrame
                .translatedToOrigin(screenOrigin)
                .flipped(in: CGFloat(configuration.viewportSize.y))
            closure(Geometry(draw: draw, frame: destinationFrame, textureFrame: textureFrame(draw),
                             source: draw.sourceFrame, destination: draw.destinationFrame))
        }

        // Now do unicode placeholders.
        DLog("enumerateOperations: processing \(runs.count) unicode placeholder runs")
        let cellSizePoints = cellConfiguration.cellSize / configuration.scale
        for (i, run) in runs.enumerated() {
            guard let draw = draw(for: run) else {
                DLog("enumerateOperations: run[\(i)] - NO DRAW FOUND for imageID=\(run.draw.imageID) placementID=\(run.draw.placementID)")
                continue
            }
            guard zRange.contains(Int(clamping: draw.zIndex)) else { continue }
            DLog("enumerateOperations: run[\(i)] imageID=\(draw.imageID) (0x\(String(draw.imageID, radix: 16))) sourceCoord=\(VT100GridCoordDescription(run.sourceCoord)) destCoord=\(VT100GridCoordDescription(run.destCoord)) - rendering")
            let instructions =
            iTermKittyPlaceholderDrawInstructionsCreate(
                draw,
                cellConfiguration.cellSize / configuration.scale,
                run.sourceCoord,
                run.destCoord,
                NSPoint(x: cellSizePoints.width * CGFloat(run.destCoord.x) + self.margins.left / configuration.scale,
                        y: cellSizePoints.height * CGFloat(run.destCoord.y)),
                draw.imageID,
                draw.placementID,
                0)
            guard instructions.valid.boolValue else {
                continue
            }
            let quadTextureFrame = instructions.sourceRect.flipped(in: draw.image.scaledSize.height) / draw.image.scaledSize
            // destRect's origin is at the bottom of the cell, while dest's y grows downward from
            // the top of the view, and translation.y is the bottom of the cell.
            var dest = NSRect(x: instructions.translation.x + instructions.destRect.minX,
                              y: instructions.translation.y - instructions.destRect.maxY,
                              width: instructions.destRect.width,
                              height: instructions.destRect.height)
            dest *= configuration.scale
            dest.shiftY(by: margins.bottom)
            guard dest.intersects(NSRect(x: 0, y: 0,
                                         width: CGFloat(configuration.viewportSize.x),
                                         height: CGFloat(configuration.viewportSize.y))) else { continue }
            closure(Geometry(draw: draw, frame: dest.flipped(in: CGFloat(configuration.viewportSize.y)),
                             textureFrame: quadTextureFrame.inverted,
                             source: instructions.sourceRect, destination: instructions.destRect))
        }
    }

    private func draw(for run: KittyImageRun) -> iTermKittyImageDraw? {
        return iTermFindKittyImageDrawForVirtualPlaceholder(draws,
                                                            run.draw.placementID,
                                                            run.draw.imageID)
    }

    private func textureFrame(_ draw: iTermKittyImageDraw) -> NSRect {
        return Self.textureFrame(sourceFrame: draw.sourceFrame, imageSize: draw.image.scaledSize)
    }

    // sourceFrame is in the image's bottom-left-origin coordinate space, as in iTermKittyImageDraw.
    // Texture coordinates have a top-left origin, so flip it first (as the placeholder path does).
    static func textureFrame(sourceFrame: NSRect, imageSize: NSSize) -> NSRect {
        return (sourceFrame.flipped(in: imageSize.height) / imageSize).inverted
    }

    fileprivate func prepareTextures(commandBuffer: MTLCommandBuffer) {
        enumerateGeometries(zRange: Int(Int32.min)..<(Int(Int32.max) + 1)) { geometry in
            _ = texture(for: geometry.draw, commandBuffer: commandBuffer)
        }
    }

    private func texture(for draw: iTermKittyImageDraw, commandBuffer: MTLCommandBuffer? = nil) -> MTLTexture? {
        if let lease = textures.value[draw.imageUniqueID] {
            frameLeases.retain(lease)
            return lease.texture
        }
        guard let lease = createTexture(draw, commandBuffer: commandBuffer) else {
            return nil
        }
        textures.value[draw.imageUniqueID] = lease
        frameLeases.retain(lease)
        return lease.texture
    }

    private func createTexture(_ draw: iTermKittyImageDraw, commandBuffer: MTLCommandBuffer?) -> ZTermTextureLease? {
        guard let image = draw.image.images.firstObject as? NSImage else {
            return nil
        }
        if let texturePipeline, let commandBuffer,
           let cgImage = draw.image.ztermSourceCGImage ?? image.bestRepresentation(
               for: NSRect(origin: .zero, size: image.size), context: nil, hints: nil)?
               .cgImage(forProposedRect: nil, context: nil, hints: nil),
           let colorSpace = configuration.colorSpace.cgColorSpace,
           let lease = texturePipeline.encode(cgImage, colorSpace: colorSpace,
                                               generation: draw.imageUniqueID, commandBuffer: commandBuffer) {
            if metrics?.enabled == true {
                metrics?.record(gpu: true, pool: texturePipeline.pool, profile: configuration.colorSpace.iccProfileData,
                                lookup: texturePipeline.lookupConversions, coreImage: texturePipeline.coreImageConversions,
                                lookupBuilds: texturePipeline.lookupBuilds, lookupBytes: texturePipeline.lookupBytes,
                                viewportWidth: Int(configuration.viewportSize.x), viewportHeight: Int(configuration.viewportSize.y),
                                scale: Double(configuration.scale))
            }
            return lease
        }
        guard let texture = cellRenderer.texture(fromImage: iTermImageWrapper(image: image),
                                                 context: poolContext,
                                                 colorSpace: configuration.colorSpace) else { return nil }
        if metrics?.enabled == true {
            metrics?.record(gpu: false, pool: texturePipeline?.pool, profile: configuration.colorSpace.iccProfileData,
                            viewportWidth: Int(configuration.viewportSize.x), viewportHeight: Int(configuration.viewportSize.y),
                            scale: Double(configuration.scale))
        }
        return ZTermTextureLease(texture: texture, generation: draw.imageUniqueID)
    }
}

@objc(iTermKittyImageRenderer)
class KittyImageRenderer: NSObject, iTermMetalCellRendererProtocol {
    @objc var rendererDisabled = false
    private let cellRenderer: iTermMetalCellRenderer
    private var textures = ReferenceContainer([UUID: ZTermTextureLease]())
    private var texturePipeline: ZTermKittyTexturePipeline?
    private var colorProfile: NSColorSpace?
    private let metrics = ZTermRenderMetrics()
    @objc var minZ: Int32 = 0
    @objc var maxZ: Int32 = 0
    @objc func discardTextures() {
        textures.value.removeAll()
        texturePipeline?.drain()
        colorProfile = nil
    }

    @objc(initWithDevice:)
    init?(device: MTLDevice) {
        let maybeCellRenderer = iTermMetalCellRenderer(device: device,
                                                       vertexFunctionName: "KittyImageVertexShader",
                                                       fragmentFunctionName: "KittyImageFragmentShader",
                                                       blending: iTermMetalBlending.compositeSourceOver(),
                                                       piuElementSize: 0,
                                                       transientStateClass: KittyImageRendererTransientState.self)
        guard let cellRenderer = maybeCellRenderer else {
            return nil
        }
        self.cellRenderer = cellRenderer
        if ProcessInfo.processInfo.environment["ZTERM2_KITTY_GPU"] == "1" {
            texturePipeline = ZTermKittyTexturePipeline(device: device)
        }
    }

    func createTransientStateStat() -> iTermMetalFrameDataStat {
        return .pqCreateImageTS
    }
    
    func draw(with frameData: iTermMetalFrameData, transientState: iTermMetalCellRendererTransientState) {
        guard let tState = transientState as? KittyImageRendererTransientState else {
            return
        }
        guard frameData.renderEncoder != nil else { return }
        let zRange = Range(Int(minZ)...Int(maxZ))
        tState.enumerateOperations(zRange: zRange) { op in
            guard let renderEncoder = frameData.renderEncoder else { return }
            cellRenderer.draw(with: transientState,
                              renderEncoder: renderEncoder,
                              numberOfVertices: 6,
                              numberOfPIUs: 0,
                              vertexBuffers: [ NSNumber(value: iTermVertexInputIndexVertices.rawValue): op.quad ],
                              fragmentBuffers: [:],
                              textures: [ NSNumber(value: iTermTextureIndexPrimary.rawValue): op.texture ])
        }
    }

    @objc(prepareTexturesForFrameData:)
    func prepareTextures(for frameData: iTermMetalFrameData) {
        guard let tState = frameData.transientState(forRenderer: self) as? KittyImageRendererTransientState,
              let commandBuffer = frameData.commandBuffer else { return }
        // Bound the texture cache to the working set. Every transmission mints a fresh Image with a
        // fresh uniqueId, and re-pointing live placements on retransmission means an animation that
        // repeatedly retransmits under one image id would otherwise add one permanent MTLTexture per
        // frame. Drop cached textures whose image is not drawn this frame; live ones are kept and
        // reused, so a stable scene never re-creates textures. Do this once per frame (not per
        // z-range pass), and only rebuild the dictionary when something is actually dead so the
        // steady state is free.
        if !tState.didPruneTextures {
            tState.didPruneTextures = true
            let liveUUIDs = tState.visibleImageIDs
            if textures.value.contains(where: { !liveUUIDs.contains($0.key) }) {
                textures.value = textures.value.filter { liveUUIDs.contains($0.key) }
            }
            if liveUUIDs.isEmpty { texturePipeline?.drain() }
        }
        tState.prepareTextures(commandBuffer: commandBuffer)
    }

    func createTransientState(forCellConfiguration configuration: iTermCellRenderConfiguration, commandBuffer: any MTLCommandBuffer) -> iTermMetalRendererTransientState? {
        let transientState = cellRenderer.createTransientState(forCellConfiguration: configuration,
                                                               commandBuffer: commandBuffer) as! KittyImageRendererTransientState
        initializeTransientState(transientState)
        let retainedLeases = transientState.frameLeases
        commandBuffer.addCompletedHandler { _ in retainedLeases.complete() }
        return transientState
    }

    private func initializeTransientState(_ tState: KittyImageRendererTransientState) {
        let profile = tState.configuration.colorSpace
        if colorProfile?.isEqual(profile) != true {
            textures.value.removeAll()
            texturePipeline?.drain()
            colorProfile = profile
        }
        tState.cellRenderer = cellRenderer;
        tState.textures = textures;
        tState.texturePipeline = texturePipeline
        tState.metrics = metrics
    }
}
