import AppKit
import objc

class TestView(AppKit.NSView):
    def isFlipped(self):
        return True

    def drawRect_(self, dirtyRect):
        AppKit.NSColor.clearColor().set()
        AppKit.NSRectFill(dirtyRect)

        AppKit.NSColor.redColor().set()
        rect = AppKit.NSMakeRect(100, 100, 400, 400)
        path = AppKit.NSBezierPath.bezierPathWithRect_(rect)
        path.setLineWidth_(5.0)
        path.stroke()
        
        # Draw tag
        font = AppKit.NSFont.userFixedPitchFontOfSize_(24.0)
        attrs = {
            AppKit.NSFontAttributeName: font,
            AppKit.NSForegroundColorAttributeName: AppKit.NSColor.blackColor()
        }
        ns_str = AppKit.NSString.stringWithString_("TEST")
        ns_str.drawInRect_withAttributes_(AppKit.NSMakeRect(100, 100, 100, 50), attrs)


def main():
    app = AppKit.NSApplication.sharedApplication()
    screen_frame = AppKit.NSScreen.mainScreen().frame()
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        screen_frame,
        AppKit.NSWindowStyleMaskBorderless,
        AppKit.NSBackingStoreBuffered,
        False
    )
    window.setOpaque_(False)
    window.setBackgroundColor_(AppKit.NSColor.clearColor())
    window.setIgnoresMouseEvents_(True)
    window.setLevel_(AppKit.NSFloatingWindowLevel)
    
    view = TestView.alloc().initWithFrame_(screen_frame)
    window.setContentView_(view)
    
    # window.setAlphaValue_(0.0)
    window.orderFront_(None)
    
    AppKit.NSAnimationContext.beginGrouping()
    AppKit.NSAnimationContext.currentContext().setDuration_(2.0)
    window.animator().setAlphaValue_(1.0)
    AppKit.NSAnimationContext.endGrouping()
    
    from PyObjCTools import AppHelper
    import threading
    import time
    def exit_soon():
        time.sleep(3)
        app.terminate_(None)
    threading.Thread(target=exit_soon).start()
    
    AppHelper.runEventLoop()

if __name__ == '__main__':
    main()
