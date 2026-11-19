// Float ping-pong render targets (RG16F) for simulations.
export interface PingPong { read: WebGLTexture; writeFbo: WebGLFramebuffer; swap(): void; upload(rg: Float32Array): void; dispose(): void }

export function createPingPong(gl: WebGL2RenderingContext, w: number, h: number): PingPong {
  const mk = () => {
    const t = gl.createTexture()!;
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RG16F, w, h, 0, gl.RG, gl.FLOAT, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.REPEAT);
    const f = gl.createFramebuffer()!;
    gl.bindFramebuffer(gl.FRAMEBUFFER, f);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, t, 0);
    const ok = gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE;
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    if (!ok && !gl.isContextLost()) throw new Error('webgl2: RG16F framebuffer incomplete');
    return { t, f };
  };
  let a = mk(), b = mk();
  return {
    get read() { return a.t; },
    get writeFbo() { return b.f; },
    swap() { [a, b] = [b, a]; },
    upload(rg) {
      gl.bindTexture(gl.TEXTURE_2D, a.t);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RG16F, w, h, 0, gl.RG, gl.FLOAT, rg);
    },
    dispose() { for (const x of [a, b]) { gl.deleteTexture(x.t); gl.deleteFramebuffer(x.f); } },
  };
}
