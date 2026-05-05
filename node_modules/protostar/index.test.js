const protostar = require(`./index`)

test(`protostar()`, () => {
  const app = protostar()
  expect(typeof (protostar)).toEqual(`function`)
  expect(app()).toEqual({})
})

test(`protostar(main)`, () => {
  const main = jest.fn((factory, state) => {
    return state
  })
  const app = protostar(main)
  expect(app()).toEqual({})
  app()
  expect(main).toHaveBeenCalledTimes(2)
})

test(`protostar(main, methods)`, () => {
  const main = jest.fn((factory, state) => {
    return state
  })
  const changeState = jest.fn((factory, state, newState) => {
    return factory(Object.assign({}, state, newState))
  })
  const app = protostar(main, { changeState })

  const newState = { name: `Kirk` }
  expect(app.changeState(newState)()).toEqual({ name: `Kirk` })
  expect(app()).toEqual({})
  expect(main).toHaveBeenCalledTimes(2)
  expect(changeState).toHaveBeenCalledTimes(1)
})

test(`protostar(main, methods, initialState)`, () => {
  const main = jest.fn((factory, state) => {
    return state
  })
  const app = protostar(main, {}, { starship: `Enterprise` })
  expect(app()).toEqual({ starship: `Enterprise` })
})
