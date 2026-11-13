#import <AppKit/AppKit.h>
#import "PTYSession.h"
#import "SessionView.h"
#import "PTYTask.h"
#import "iTermPreferences.h"
#import "iTermProfilePreferences.h"
#import "iTermUserDefaults.h"
#import "TaskNotifier.h"
#import "iTermAdvancedSettingsModel.h"
#import "iTermApplication.h"
#import "iTermBuiltInFunctions.h"
#import "PTYTab.h"
#import "iTermResourceLimitsHelper.h"
#include <sys/wait.h>

static inline int zterm2_exit_code(int status) {
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

static inline int zterm2_exit_signal(int status) {
    return WIFSIGNALED(status) ? WTERMSIG(status) : 0;
}

static inline void zterm2_drain_screen(VT100Screen *screen, dispatch_block_t completion) {
    [screen mutateAsynchronously:^(VT100Terminal *terminal, VT100ScreenMutableState *state, id<VT100ScreenDelegate> delegate) {
        dispatch_async(dispatch_get_main_queue(), completion);
    }];
}
