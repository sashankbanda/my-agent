import React from "react";
import { createRoot } from "react-dom/client";
import Hud from "./hud/Hud";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Hud />
  </React.StrictMode>,
);
