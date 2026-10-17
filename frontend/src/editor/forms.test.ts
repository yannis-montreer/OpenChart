import { describe, expect, it } from "vitest";

import type { TemplateInfo } from "../api";
import { MAX_SOURCE_BYTES } from "./rules";
import { checkDuplicate, checkImport, checkNew, checkRename, idFromTitle, suggestionText } from "./forms";

const TEMPLATES: TemplateInfo[] = [
  { id: "blank", title: "Blank", description: "" },
  { id: "sma_cross", title: "SMA cross", description: "" },
];

describe("idFromTitle", () => {
  it("makes an id to start from, as you type a title", () => {
    expect(idFromTitle("SMA cross")).toBe("sma_cross");
    expect(idFromTitle("  RSI (14) with bands! ")).toBe("rsi_14_with_bands");
    expect(idFromTitle("9 lives")).toBe("x9_lives");
    expect(idFromTitle("a".repeat(60))).toBe("a".repeat(40));
    expect(idFromTitle("Étoile")).toBe("toile");
  });

  it("is empty when nothing usable is left, never an invalid id", () => {
    expect(idFromTitle("")).toBe("");
    expect(idFromTitle("***")).toBe("");
    expect(idFromTitle("日本語")).toBe("");
  });
});

describe("checkNew", () => {
  const ok = { id: "my_cross", template: "sma_cross", title: "My cross" };

  it("is clean for a good form", () => {
    expect(checkNew(ok, TEMPLATES, [])).toEqual({});
  });

  it("lowercases what is typed, and refuses the rest before any request", () => {
    expect(checkNew({ ...ok, id: "My_Cross" }, TEMPLATES, [])).toEqual({});
    expect(checkNew({ ...ok, id: "my cross" }, TEMPLATES, []).id).toBe("Use only lowercase letters, digits and _.");
    expect(checkNew({ ...ok, id: "../x" }, TEMPLATES, []).id).toBeDefined();
    expect(checkNew({ ...ok, id: "" }, TEMPLATES, []).id).toBe("Give it a name.");
  });

  it("says when the name is taken, and what is free", () => {
    expect(checkNew(ok, TEMPLATES, ["my_cross", "my_cross_2"]).id).toBe("There is already a file named my_cross. Try my_cross_3.");
  });

  it("is case-insensitive about a taken name", () => {
    expect(checkNew({ ...ok, id: "MY_CROSS" }, TEMPLATES, ["my_cross"]).id).toMatch(/already a file/);
  });

  it("a file whose name has capitals still takes the name (a Windows folder does not tell them apart)", () => {
    expect(checkNew(ok, TEMPLATES, ["My_Cross"]).id).toMatch(/already a file named my_cross/);
  });

  it("wants a template that exists, and a good title when one is given", () => {
    expect(checkNew({ ...ok, template: "nope" }, TEMPLATES, []).template).toBe("Pick a template.");
    expect(checkNew({ ...ok, template: "" }, TEMPLATES, []).template).toBe("Pick a template.");
    expect(checkNew({ ...ok, title: "a\nb" }, TEMPLATES, []).title).toBe("A title is one line.");
    expect(checkNew({ ...ok, title: "" }, TEMPLATES, [])).toEqual({}); // no title: the template's own
  });
});

describe("checkRename", () => {
  it("wants a title of 1 to 60 characters on one line", () => {
    expect(checkRename({ title: "Better" }, "Old")).toEqual({});
    expect(checkRename({ title: " " }, "Old").title).toBe("Give it a title.");
    expect(checkRename({ title: "x".repeat(61) }, "Old").title).toBe("At most 60 characters.");
  });

  it("says when nothing changes", () => {
    expect(checkRename({ title: "Old" }, "Old").title).toBe("That is the title already.");
  });
});

describe("checkDuplicate", () => {
  it("wants a free good id and an optional good title", () => {
    expect(checkDuplicate({ id: "copy_of_x", title: "" }, ["x"])).toEqual({});
    expect(checkDuplicate({ id: "x", title: "" }, ["x"]).id).toMatch(/already a file named x/);
    expect(checkDuplicate({ id: "Bad id", title: "" }, []).id).toBeDefined();
    expect(checkDuplicate({ id: "ok", title: "a\nb" }, []).title).toBe("A title is one line.");
  });

  it("leaves an empty id to the server (the first free one)", () => {
    expect(checkDuplicate({ id: "", title: "" }, ["x", "x_2"])).toEqual({});
    expect(checkDuplicate({ id: "  ", title: "" }, [])).toEqual({});
  });
});

describe("checkImport", () => {
  const file = { name: "My RSI.py", size: 100 };

  it("shows the id the server will make of the file name, and takes a typed one over it", () => {
    expect(checkImport({ id: "" }, file, [])).toEqual({ id: undefined, problems: {}, effective: "my_rsi" });
    expect(checkImport({ id: "Mine" }, file, []).effective).toBe("mine");
  });

  it("refuses a file that is not .py or over the cap before it is read", () => {
    expect(checkImport({ id: "" }, { name: "x.txt", size: 1 }, []).problems.file).toBe("Pick a .py file.");
    expect(checkImport({ id: "" }, { name: "x.py", size: MAX_SOURCE_BYTES + 1 }, []).problems.file).toMatch(/256 KB/);
    expect(checkImport({ id: "" }, null, []).problems.file).toBe("Pick a .py file.");
  });

  it("says when the id is taken and offers a free one: an import never overwrites", () => {
    expect(checkImport({ id: "" }, file, ["my_rsi"]).problems.id).toBe("There is already a file named my_rsi. Try my_rsi_2.");
    expect(checkImport({ id: "other" }, file, ["my_rsi"]).problems).toEqual({});
  });
});

describe("suggestionText", () => {
  it("reads the free id a 409 suggests", () => {
    expect(suggestionText({ message: "exists", suggested: "mine_2" })).toBe("mine_2");
    expect(suggestionText({ message: "exists" })).toBeNull();
    expect(suggestionText(undefined)).toBeNull();
    expect(suggestionText("text")).toBeNull();
    expect(suggestionText({ suggested: "Not An Id" })).toBeNull();
    expect(suggestionText({ suggested: 5 })).toBeNull();
  });
});
