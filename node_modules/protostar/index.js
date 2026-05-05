const factory = (main, methods, state) => Object.assign(
  main.bind(null, factory.bind(null, main, methods), state),
  Object.keys(methods)
  .reduce((api, name) => Object.assign(
    api,
    {
      [name]: methods[name].bind(
        null,
        factory.bind(null, main, methods),
        state
      )
    }
  ), {})
)

module.exports = (
  main = (factory, state) => state,
  methods = {},
  initialState = {}
) => factory(main, methods, initialState)
