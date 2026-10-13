import "./styles/main.css";
import { App } from "./app";

const app = new App(document.getElementById("app")!);
// Handy in the browser console when something looks wrong.
(window as unknown as { openchart: App }).openchart = app;
void app.start();
