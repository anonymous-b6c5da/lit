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


#include <optix.h>
#include "optix_denoiser_opticalflow.cuh"

extern "C" OptixResult runOpticalFlow( CUcontext ctx, CUstream stream, OptixImage2D & flow, OptixImage2D input[2], float & flowTime, std::string & errMessage )
{
    OptixUtilOpticalFlow oflow;

    if( const OptixResult res = oflow.init( ctx, stream, input[0].width, input[0].height ) )
    {
        oflow.getLastError( errMessage );
        return res;
    }

    CUevent start, stop;
    cuEventCreate( &start, 0 );
    cuEventCreate( &stop, 0 );
    cuEventRecord( start, stream );
    
    if( const OptixResult res = oflow.computeFlow( flow, input ) )
    {
        oflow.getLastError( errMessage );
        cuEventDestroy( start );
        cuEventDestroy( stop );
        return res;
    }

    cuEventRecord(stop, stream);
    cuEventSynchronize( stop );
    cuEventElapsedTime(&flowTime, start, stop);

    cuEventDestroy( start );
    cuEventDestroy( stop );

    return oflow.destroy();
}
