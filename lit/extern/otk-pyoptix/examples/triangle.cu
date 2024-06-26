//
// Copyright (c) 2021, NVIDIA CORPORATION. All rights reserved.
//
// Redistribution and use in source and binary forms, with or without
// modification, are permitted provided that the following conditions
// are met:
//  * Redistributions of source code must retain the above copyright
//    notice, this list of conditions and the following disclaimer.
//  * Redistributions in binary form must reproduce the above copyright
//    notice, this list of conditions and the following disclaimer in the
//    documentation and/or other materials provided with the distribution.
//  * Neither the name of NVIDIA CORPORATION nor the names of its
//    contributors may be used to endorse or promote products derived
//    from this software without specific prior written permission.
//
// THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS ``AS IS'' AND ANY
// EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
// IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
// PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR
// CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
// EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
// PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
// PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY
// OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
// (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
// OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
//

#include <math_constants.h>
#include <optix.h>

#include "helpers.h"
#include "triangle.h"
#include "vec_math.h"

extern "C" {
__constant__ Params params;
}

static __forceinline__ __device__ void setPayload(float3 p) {
    // Payload registers takes 32-bit integer values, float_as_int casts a float
    // to a 32-bit integer with bits preserved.
    optixSetPayload_0(float_as_int(p.x));
    optixSetPayload_1(float_as_int(p.y));
    optixSetPayload_2(float_as_int(p.z));
}

static __forceinline__ __device__ void computeRay(uint3 idx,
                                                  uint3 dim,
                                                  float3& origin,
                                                  float3& direction) {
    // const float3 U = params.cam_u;
    // const float3 V = params.cam_v;
    // const float3 W = params.cam_w;
    // const float2 d = 2.0f * make_float2(static_cast<float>(idx.x) /
    //                                             static_cast<float>(dim.x),
    //                                     static_cast<float>(idx.y) /
    //                                             static_cast<float>(dim.y)) -
    //                  1.0f;

    // origin = params.cam_eye;
    // direction = normalize(d.x * U + d.y * V + W);
    origin = params.rays_o[idx.y * params.width + idx.x];
    direction = params.rays_d[idx.y * params.width + idx.x];
}

extern "C" __global__ void __raygen__rg() {
    // Lookup our location within the launch grid
    const uint3 idx = optixGetLaunchIndex();
    const uint3 dim = optixGetLaunchDimensions();

    // Map our launch idx to a screen location and create a ray from the camera
    // location through the screen
    float3 ray_origin, ray_direction;
    computeRay(make_uint3(idx.x, idx.y, 0), dim, ray_origin, ray_direction);

    // Trace the ray against our scene hierarchy
    unsigned int p0, p1, p2;
    optixTrace(params.handle,             // See Params class in triangle.h
               ray_origin,                // float3
               ray_direction,             // float3
               0.0f,                      // Min intersection distance
               1e16f,                     // Max intersection distance
               0.0f,                      // rayTime -- used for motion blur
               OptixVisibilityMask(255),  // Specify always visible
               OPTIX_RAY_FLAG_NONE,
               0,   // SBT offset   -- See SBT discussion
               1,   // SBT stride   -- See SBT discussion
               0,   // missSBTIndex -- See SBT discussion
               p0,  // optixSetPayload_0, returned from hit or miss kernel
               p1,  // optixSetPayload_1, returned from hit or miss kernel
               p2   // optixSetPayload_2, returned from hit or miss kernel
    );

    // Convert the ray cast result values back to floats.
    float3 result = make_float3(0);
    result.x = int_as_float(p0);
    result.y = int_as_float(p1);
    result.z = int_as_float(p2);

    // Record results in our output raster
    params.image[idx.y * params.width + idx.x] = result;
}

extern "C" __global__ void __miss__ms() {
    MissData* miss_data = reinterpret_cast<MissData*>(optixGetSbtDataPointer());
    // https://stackoverflow.com/a/15514595/1255535
    setPayload(make_float3(CUDART_INF_F, CUDART_INF_F, CUDART_INF_F));
}

extern "C" __global__ void __closesthit__ch() {
    // When built-in triangle intersection is used, a number of fundamental
    // attributes are provided by the OptiX API, indlucing barycentric
    // coordinates.
    const float2 barycentrics = optixGetTriangleBarycentrics();
    setPayload(make_float3(barycentrics, 1.0f));

    // Compute intersection point coordinates.
    const float3 ray_origin = optixGetWorldRayOrigin();
    const float3 ray_direction = optixGetWorldRayDirection();

    // Get the hit distance.
    const float t = optixGetRayTmax();

    // Compute the intersection point.
    const float3 p = ray_origin + t * ray_direction;

    setPayload(p);
}
