#include <metal_stdlib>
using namespace metal;

// Core Graphics prepares this ICC lookup once. Each frame only uploads its raw
// RGBA bytes; colour conversion and alpha handling stay on the GPU.
vertex float4 ZTermKittyConvertVertex(uint index [[vertex_id]]) {
    const float2 positions[] = { float2(-1.0f, -1.0f), float2(3.0f, -1.0f), float2(-1.0f, 3.0f) };
    return float4(positions[index], 0.0f, 1.0f);
}

fragment float4 ZTermKittyConvertFragment(float4 fragmentPosition [[position]],
                                        device const uchar4 *pixels [[buffer(0)]],
                                        constant uint4 &shape [[buffer(1)]],
                                        texture3d<float, access::sample> lookup [[texture(0)]]) {
    uint2 position = uint2(fragmentPosition.xy);
    if (position.x >= shape.x || position.y >= shape.y) { return float4(0.0f); }
    float4 value = float4(pixels[position.y * shape.x + position.x]) / 255.0f;
    if (shape.z != 0 && value.a > 0.0f) { value.rgb /= value.a; }
    constexpr sampler colourSampler(coord::normalized, address::clamp_to_edge, filter::linear);
    float size = float(lookup.get_width());
    float3 coordinate = (clamp(value.rgb, 0.0f, 1.0f) * (size - 1.0f) + 0.5f) / size;
    float3 colour = lookup.sample(colourSampler, coordinate).rgb;
    return float4(colour * value.a, value.a);
}

#import "iTermShaderTypes.h"

typedef struct {
    float4 clipSpacePosition [[position]];
    float2 textureCoordinate;
} iTermKittyImageVertexFunctionOutput;

vertex iTermKittyImageVertexFunctionOutput
KittyImageVertexShader(uint vertexID [[ vertex_id ]],
                           constant iTermVertex *vertexArray [[ buffer(iTermVertexInputIndexVertices) ]],
                           constant vector_uint2 *viewportSizePointer  [[ buffer(iTermVertexInputIndexViewportSize) ]],
                           unsigned int iid [[instance_id]]) {
    iTermKittyImageVertexFunctionOutput out;

    float2 pixelSpacePosition = vertexArray[vertexID].position.xy;
    float2 viewportSize = float2(*viewportSizePointer);

    out.clipSpacePosition.xy = pixelSpacePosition / viewportSize;
    out.clipSpacePosition.z = 0.0;
    out.clipSpacePosition.w = 1;

    out.textureCoordinate = vertexArray[vertexID].textureCoordinate;

    return out;
}

fragment float4
KittyImageFragmentShader(iTermKittyImageVertexFunctionOutput in [[stage_in]],
                         texture2d<float> texture [[ texture(iTermTextureIndexPrimary) ]]) {
    // return float4(1,0,0,1);
    constexpr sampler textureSampler (mag_filter::linear,
                                      min_filter::linear);

    if (in.textureCoordinate.x < 0.0 ||
        in.textureCoordinate.x > 1.0 ||
        in.textureCoordinate.y < 0.0 ||
        in.textureCoordinate.y > 1.0) {
        return float4(0.0, 0.0, 0.0, 0.0);
    }
    return texture.sample(textureSampler, in.textureCoordinate);
}
