"use strict";

const healthEl = document.getElementById("health");
const form = document.getElementById("echo-form");
const messageInput = document.getElementById("message");
const resultEl = document.getElementById("result");

async function checkHealth() {
    try {
        const res = await fetch("/api/health");
        const data = await res.json();
        healthEl.textContent = `Health: ${data.status}`;
    } catch (err) {
        healthEl.textContent = "Health: unavailable";
    }
}

form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = messageInput.value.trim();
    if (!message) {
        return;
    }

    resultEl.textContent = "Sending...";
    try {
        const res = await fetch("/api/echo", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message }),
        });
        const data = await res.json();
        resultEl.textContent = JSON.stringify(data, null, 2);
    } catch (err) {
        resultEl.textContent = "Error: " + err.message;
    }
});

checkHealth();
