#import <AppKit/AppKit.h>

NS_ASSUME_NONNULL_BEGIN

typedef NS_ENUM(NSInteger, ZTermEmbeddedError) {
    ZTermEmbeddedErrorRuntimeNotPrepared = 1,
    ZTermEmbeddedErrorIncompatibleRuntime = 2,
    ZTermEmbeddedErrorInvalidConfiguration = 3,
    ZTermEmbeddedErrorInvalidState = 4,
    ZTermEmbeddedErrorEngineInitializationFailed = 5,
    ZTermEmbeddedErrorWouldBlock = 6
};

__attribute__((objc_subclassing_restricted))
@interface ZTermEmbeddedController : NSViewController
@property (class, nonatomic, readonly, copy) NSString *sdkVersion;
@property (nonatomic, readonly, copy) NSUUID *instanceID;
@property (nonatomic, copy, nullable) void (^eventHandler)(NSDictionary<NSString *, id> *event);
@property (nonatomic, readonly) uint64_t eventSequence;
@property (nonatomic, readonly, copy, nullable) NSDictionary<NSString *, NSNumber *> *terminalMetrics;
+ (BOOL)prepareCPUPrototypeRuntimeWithSuiteName:(NSString *)suiteName error:(NSError **)error
    NS_SWIFT_NAME(prepareCPUPrototypeRuntime(suiteName:));
- (nullable instancetype)initWithWidth:(CGFloat)width height:(CGFloat)height error:(NSError **)error;
- (instancetype)initWithNibName:(nullable NSNibName)nibName bundle:(nullable NSBundle *)bundle NS_UNAVAILABLE;
- (nullable instancetype)initWithCoder:(NSCoder *)coder NS_UNAVAILABLE;
- (BOOL)launchExecutable:(NSURL *)executable arguments:(NSArray<NSString *> *)arguments
             environment:(NSDictionary<NSString *, NSString *> *)environment
        workingDirectory:(NSURL *)workingDirectory error:(NSError **)error;
- (nullable NSNumber *)sendInput:(NSData *)data error:(NSError **)error;
- (nullable NSString *)copyTextWithMaximumCharacters:(NSInteger)maximumCharacters error:(NSError **)error
    NS_SWIFT_NAME(copyText(maximumCharacters:));
- (void)refreshLayout;
- (void)focusTerminal;
- (void)requestStop;
@end

NS_ASSUME_NONNULL_END
