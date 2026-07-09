// Client module for the golden-fixture repo (JavaScript, regex-parsed).

import { helper } from "./util.js";

class ApiClient {
  constructor(baseUrl) {
    this.baseUrl = baseUrl;
  }

  request(path) {
    return helper(this.baseUrl + path);
  }
}

function buildClient(url) {
  return new ApiClient(url);
}

const doubler = (n) => n * 2;

export { ApiClient, buildClient, doubler };
