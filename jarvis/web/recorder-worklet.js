// Copies microphone frames to the main thread. Runs on the audio rendering thread.
// Forwarding defaults on so a caller that never sends a control message still
// receives every block. voice.js posts {on:false} until it is actually consuming
// frames, then {on:true} for calibration, wait/pre-roll, and the take.
class Recorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.forward = true;
    this.port.onmessage = (event) => {
      const data = event.data;
      if (data && typeof data.on === "boolean") this.forward = data.on;
    };
  }
  process(inputs) {
    if (!this.forward) return true;
    const ch = inputs[0] && inputs[0][0];
    if (ch) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor("jarvis-recorder", Recorder);
