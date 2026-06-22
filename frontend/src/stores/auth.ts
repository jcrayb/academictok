import { atom } from "nanostores";
import { auth, onAuthStateChanged, type User } from "../lib/firebase";

export const $user = atom<User | null>(null);
export const $loading = atom<boolean>(true);

// Single shared listener — keeps all islands in sync.
if (typeof window !== "undefined") {
  onAuthStateChanged(auth, (user) => {
    $user.set(user);
    $loading.set(false);
  });
}

/** Returns a fresh Firebase ID token for the current user. */
export async function getIdToken(forceRefresh = false): Promise<string> {
  const user = $user.get();
  if (!user) throw new Error("Not authenticated");
  return user.getIdToken(forceRefresh);
}
