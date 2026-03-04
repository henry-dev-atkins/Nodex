const TOKEN = window.__CODEX_UI_TOKEN__;

async function parseJson(response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = body?.error?.message || `${response.status} ${response.statusText}`;
    throw new Error(message);
  }
  return body;
}

export async function apiGet(path) {
  const response = await fetch(path, {
    headers: {
      Authorization: `Bearer ${TOKEN}`,
    },
  });
  return parseJson(response);
}

export async function apiPost(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  return parseJson(response);
}

export async function apiPatch(path, body) {
  const response = await fetch(path, {
    method: "PATCH",
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  return parseJson(response);
}

export async function apiDelete(path) {
  const response = await fetch(path, {
    method: "DELETE",
    headers: {
      Authorization: `Bearer ${TOKEN}`,
    },
  });
  return parseJson(response);
}

export function getToken() {
  return TOKEN;
}
