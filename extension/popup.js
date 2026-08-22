const addrInput = document.getElementById("addr");

chrome.storage.local.get(["serverAddr"], (data) => {
  addrInput.value = data.serverAddr || "http://localhost:8787";
});

document.getElementById("open").addEventListener("click", () => {
  const addr = addrInput.value.trim().replace(/\/$/, "");
  if (!addr) return;
  chrome.storage.local.set({ serverAddr: addr });
  chrome.tabs.create({ url: addr + "/sender.html" });
});
