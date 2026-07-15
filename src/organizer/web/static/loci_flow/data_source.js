import fixtureState from "./fixtures.json" with { type: "json" };

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

export function createFixtureDataSource() {
  return Object.freeze({
    kind: "fixture",
    load() {
      return clone(fixtureState);
    },
  });
}
