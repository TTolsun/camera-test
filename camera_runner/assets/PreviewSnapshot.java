package cameratest;
import com.android.uiautomator.testrunner.UiAutomatorTestCase;
import com.android.uiautomator.core.Configurator;

public class PreviewSnapshot extends UiAutomatorTestCase {
    public void testSnapshot() {
        String file = getParams().getString("file");
        if (file == null || !file.matches("camera-test-[a-zA-Z0-9-]+[.]xml")) {
            throw new IllegalArgumentException("Unexpected snapshot filename");
        }
        Configurator.getInstance().setWaitForIdleTimeout(0);
        java.io.File dir = new java.io.File(android.os.Environment.getDataDirectory(), "local/tmp");
        if (!dir.isDirectory() && !dir.mkdirs()) throw new IllegalStateException("Output folder unavailable");
        java.io.File output = new java.io.File(dir, file);
        for (int i=0;i<10;i++) {
            android.os.SystemClock.sleep(200);
            getUiDevice().dumpWindowHierarchy(file);
            if (output.length()>0) {
                java.io.File dest = new java.io.File("/data/local/tmp", file);
                if (!output.equals(dest) && !output.renameTo(dest)) throw new IllegalStateException("Move failed");
                return;
            }
        }
        fail("Camera preview snapshot unavailable");
    }
}
