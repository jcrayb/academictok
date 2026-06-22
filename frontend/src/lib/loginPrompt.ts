import { signInWithGoogle } from "./firebase";
import { verifyUser } from "./api";

let backdrop: HTMLDivElement | null = null;

function ensureBackdrop(): HTMLDivElement {
  if (backdrop) return backdrop;
  backdrop = document.createElement("div");
  backdrop.className = "settings-backdrop";
  backdrop.hidden = true;
  backdrop.innerHTML = `
    <div class="settings-panel" role="dialog" aria-modal="true" aria-label="Sign in">
      <p id="login-prompt-message" class="m-0 mb-3"></p>
      <div class="flex justify-end gap-[0.6rem]">
        <button id="login-prompt-cancel" type="button">Cancel</button>
        <button id="login-prompt-signin" type="button" class="primary">Sign in with Google</button>
      </div>
    </div>`;
  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) hide();
  });
  document.body.appendChild(backdrop);
  return backdrop;
}

function hide() {
  if (backdrop) backdrop.hidden = true;
}

/** Show a "sign in to do X" modal. Resolves true if the user signed in,
 *  false if they dismissed it instead — callers can re-check `$user` after. */
export function promptLogin(message: string): Promise<boolean> {
  const el = ensureBackdrop();
  el.querySelector("#login-prompt-message")!.textContent = message;
  el.hidden = false;

  return new Promise((resolve) => {
    const cancelBtn = el.querySelector("#login-prompt-cancel") as HTMLButtonElement;
    const signinBtn = el.querySelector("#login-prompt-signin") as HTMLButtonElement;

    function cleanup(result: boolean) {
      cancelBtn.onclick = null;
      signinBtn.onclick = null;
      hide();
      resolve(result);
    }

    cancelBtn.onclick = () => cleanup(false);
    signinBtn.onclick = async () => {
      signinBtn.disabled = true;
      try {
        await signInWithGoogle();
        await verifyUser();
        cleanup(true);
      } catch (e) {
        console.error(e);
        signinBtn.disabled = false;
      }
    };
  });
}
