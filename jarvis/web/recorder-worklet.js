// Copies microphone frames to the main thread. Runs on the audio rendering thread.
class Recorder extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor("jarvis-recorder", Recorder);
