import jsQR from "jsqr";
import { useEffect, useRef, useState } from "react";

interface QrScannerProps {
  onScan: (payload: string) => void;
  onClose: () => void;
}

/** Reads the QR code the app shows (payload "CP1:<reservation>:<code>"), from
 *  the kiosk camera. Without a camera (or permission), the payload can be typed
 *  or pasted, which is also how desktop demos use it. */
export function QrScanner({ onScan, onClose }: QrScannerProps) {
  const video = useRef<HTMLVideoElement>(null);
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [typed, setTyped] = useState("");

  useEffect(() => {
    let stream: MediaStream | null = null;
    let frame = 0;
    let stopped = false;
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d", { willReadFrequently: true });

    const scan = (): void => {
      const element = video.current;
      if (stopped || element === null || context === null) return;
      if (element.readyState === element.HAVE_ENOUGH_DATA) {
        canvas.width = element.videoWidth;
        canvas.height = element.videoHeight;
        context.drawImage(element, 0, 0);
        const image = context.getImageData(0, 0, canvas.width, canvas.height);
        const found = jsQR(image.data, image.width, image.height);
        if (found !== null && found.data.startsWith("CP1:")) {
          onScan(found.data);
          return;
        }
      }
      frame = requestAnimationFrame(scan);
    };

    if (navigator.mediaDevices?.getUserMedia === undefined) {
      setCameraError("No camera on this device.");
    } else {
      navigator.mediaDevices
        .getUserMedia({ video: { facingMode: "user" } })
        .then((media) => {
          stream = media;
          if (stopped || video.current === null) return;
          video.current.srcObject = media;
          void video.current.play();
          frame = requestAnimationFrame(scan);
        })
        .catch(() => setCameraError("Camera unavailable."));
    }
    return () => {
      stopped = true;
      cancelAnimationFrame(frame);
      stream?.getTracks().forEach((track) => track.stop());
    };
  }, [onScan]);

  return (
    <div className="modal" role="dialog" aria-label="Scan QR code">
      <div className="modal-card">
        <h2>Hold the QR code from the app up to the camera</h2>
        {cameraError === null ? (
          <video ref={video} className="camera" muted playsInline />
        ) : (
          <p className="muted">{cameraError}</p>
        )}
        <form
          className="qr-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (typed.trim()) onScan(typed.trim());
          }}
        >
          <input
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            placeholder="…or paste the QR content (CP1:…)"
            aria-label="QR content"
            maxLength={64}
          />
          <button type="submit" className="button primary">
            Use
          </button>
        </form>
        <button type="button" className="button" onClick={onClose}>
          Cancel
        </button>
      </div>
    </div>
  );
}
