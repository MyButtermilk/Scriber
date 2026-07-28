#ifndef SCRIBER_MSVC_STDALIGN_H
#define SCRIBER_MSVC_STDALIGN_H

#if !defined(__cplusplus)
#define alignas _Alignas
#define alignof _Alignof
#endif

#define __alignas_is_defined 1
#define __alignof_is_defined 1

#endif
