import { createStore } from "/js/AlpineStore.js";
import * as api from "/js/api.js";

export const store = createStore("treeSitterInspector", {
  filePath: "",
  rootPath: "",
  language: "",
  query: "",
  loading: false,
  reindexing: false,
  installing: false,
  depsNeeded: false,
  error: "",
  inspection: null,
  indexStatus: null,

  onOpen() {
    this.error = "";
    this.inspection = null;
    this.indexStatus = null;
    this.checkDeps();
  },

  cleanup() {},


  async checkDeps() {
    try {
      await api.callJsonApi("/plugins/tree_sitter/index_status", {
        root_path: "/tmp",
      });
      this.depsNeeded = false;
    } catch (error) {
      const msg = error.message || String(error);
      this.depsNeeded = msg.includes("tree-sitter-language-pack");
    }
  },

  async installDeps() {
    this.installing = true;
    this.error = "";
    try {
      await api.callJsonApi("/plugins/tree_sitter/install_deps", {});
      this.depsNeeded = false;
    } catch (error) {
      this.error = error.message || String(error);
    } finally {
      this.installing = false;
    }
  },


  async inspectFile() {
    if (!this.filePath) {
      this.error = "Enter a file path to inspect.";
      return;
    }

    this.loading = true;
    this.error = "";
    try {
      this.inspection = await api.callJsonApi("/plugins/tree_sitter/inspect", {
        path: this.filePath,
        language: this.language || undefined,
        query: this.query || undefined,
      });
    } catch (error) {
      this.error = error.message || String(error);
    } finally {
      this.loading = false;
    }
  },

  async refreshIndexStatus() {
    if (!this.rootPath) {
      this.error = "Enter a repo root to inspect index status.";
      return;
    }

    this.error = "";
    try {
      const response = await api.callJsonApi("/plugins/tree_sitter/index_status", {
        root_path: this.rootPath,
      });
      this.indexStatus = response.status;
    } catch (error) {
      this.error = error.message || String(error);
    }
  },

  async reindex() {
    if (!this.rootPath) {
      this.error = "Enter a repo root before reindexing.";
      return;
    }

    this.reindexing = true;
    this.error = "";
    try {
      this.indexStatus = await api.callJsonApi("/plugins/tree_sitter/reindex", {
        root_path: this.rootPath,
      });
    } catch (error) {
      this.error = error.message || String(error);
    } finally {
      this.reindexing = false;
    }
  },

  formatJson(value) {
    return value ? JSON.stringify(value, null, 2) : "";
  },
});
